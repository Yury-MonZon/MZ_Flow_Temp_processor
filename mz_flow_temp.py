#!/usr/bin/env python
"""mz_flow_temp.py

Author: Yury MonZon

A Python post-processor for 3D printer G-code files, implementing flow and temperature smoothing for improved print quality.

Exit/Error codes:
    0   Success
    1   Incorrect usage (missing arguments)
    2   Input file not found
    3   Missing EXECUTABLE_BLOCK markers
    4   Missing MZ FLOW TEMP markers inside EXECUTABLE_BLOCK
    5   Error parsing settings from G-code
    6   Missing required parameters in G-code/config
    7   No moves found in G-code
    8   Error writing processed G-code file
    9   Unhandled exception

Features:
- Analyzes G-code to extract extrusion moves and calculates flow rates.
- Smooths flow and dynamically adjusts nozzle temperature based on predicted flow demand.
- Clamps feedrate to avoid exceeding the printer's maximum volumetric flow capability.
- Supports real-time plotting of flow and temperature profiles during processing.
- Integrates with popular slicers (OrcaSlicer, PrusaSlicer, SuperSlicer, Bambu Studio, Cura) as a post-processing script.
- Reads required print and script-specific parameters from G-code comments or config blocks.
- Optionally relaunches the slicer or viewer after processing.
Usage:
    python mz_flow_temp.py <input.gcode>
Requirements:
- Python 3
- numpy
- matplotlib
- PyQt5
- psutil

Install dependencies:
Linux: sudo apt install python3-matplotlib python3-numpy python3-pyqt5 python3-psutil
Win: pip install matplotlib numpy PyQt5 psutil

------------------ SLICER INTEGRATION GUIDE ------------------
To use this script as a post-processor in your slicer, add the following lines
to your slicer's post-processing command/script section.

For OrcaSlicer, PrusaSlicer, or SuperSlicer:
1. Go to Printer Settings > Output options > Post-processing scripts.
2. Add a new script with the following command (adjust the path as needed):
   python3 <path to script>/mz_flow_temp.py

For Bambu Studio:
1. Go to Preferences > Post-processing.
2. Add a new script with the command above.

For Cura:
1. Go to Extensions > Post Processing > Modify G-Code > Add a script > "Run a script".
2. Enter the command above.

Make sure your G-code profile includes the required parameters:
  ; nozzle_temperature_range_high = 260
  ; nozzle_temperature_range_low = 220
  ; filament_diameter = 1.75
  ; slow_down_min_speed = 30
  ; filament_max_volumetric_speed = 12
  ; nozzle_temperature_initial_layer = 240
  ; initial_layer_print_height = 0.2

And in your printer_notes or config block, include:
  mz_flow_temp_sec_per_c_heating = 6
  mz_flow_temp_sec_per_c_cooling = 4
  mz_flow_temp_launch_viewer = true
--------------------------------------------------------------

"""
import sys
import re
import math
import os
import numpy as np
import psutil
import subprocess
import time
import logging
import matplotlib
from datetime import datetime
matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
plt.style.use('ggplot')

# Setup logging
# Get current date/time for log file name
now_str = datetime.now().strftime('%Y%m%d_%H%M%S')
gcode_file = sys.argv[1] if len(sys.argv) > 1 else 'unknown'
gcode_base = os.path.splitext(os.path.basename(gcode_file))[0]
log_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    f'mz_flow_temp_{now_str}_{gcode_base}.log'
)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(log_path, mode='w', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

RE_MOVE = re.compile(r'G0?1[ \t]+([^;]*)', re.IGNORECASE)
RE_X = re.compile(r'X([-+]?[0-9]*\.?[0-9]+)')
RE_Y = re.compile(r'Y([-+]?[0-9]*\.?[0-9]+)')
RE_Z = re.compile(r'Z([-+]?[0-9]*\.?[0-9]+)')
RE_E = re.compile(r'E([-+]?[0-9]*\.?[0-9]+)')
RE_F = re.compile(r'F([-+]?[0-9]*\.?[0-9]+)')

def filament_area(d):
    return math.pi * (d/2)**2

class Move:
    __slots__ = ('x', 'y', 'z', 'e', 'f', 'extruding', 'line_num', 'raw',
                'flow', 'smoothed_flow', 'smoothed_temp', 'clamped_feedrate',
                'feedrate_was_clamped', 'max_allowed_flow', 'final_flow',
                'move_time', 'dist_xyz', 'global_time')
    def __init__(self, x, y, z, e, f, extruding, line_num, raw):
        self.x = x
        self.y = y
        self.z = z
        self.e = e
        self.f = f
        self.extruding = extruding
        self.line_num = line_num
        self.raw = raw
        self.flow = 0.0
        self.smoothed_flow = 0.0
        self.smoothed_temp = settings['nozzle_temperature_range_low']
        self.clamped_feedrate = f
        self.feedrate_was_clamped = False
        self.max_allowed_flow = 0.0
        self.final_flow = 0.0
        self.move_time = 0.0
        self.dist_xyz = 0.0
        self.global_time = 0.0

plotting_data = {
    'times': [],
    'flows': [],
    'final_flows': [],
    'final_temps': [],
    'ideal_temps': []
}
plot_fig = None
plot_axes = None
plot_lines = {}

def get_parent_process_info():
    """Return (name, exe_path) of the parent or grandparent process, or (None, None) if unavailable."""
    try:
        parent = psutil.Process(os.getppid())
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logging.warning(f"Could not access parent process: {e}")
        return None, None

    grandparent = parent.parent()
    if grandparent.exe().endswith('.exe'):
        target_proc = parent  # windows
    else:
        target_proc = grandparent if grandparent else parent  # nix

    try:
        exe_path = target_proc.exe()
        logging.info(f"Checked process executable path: {exe_path}")
        slicer_names = ['orca', 'prusa', 'cura', 'super', 'bambu', 'ideamaker', 'slic3r']
        exe_path_lower = exe_path.lower()
        if any(slicer in exe_path_lower for slicer in slicer_names):
            logging.info(f"Recognized slicer process: {exe_path}")
            return 'slicer', exe_path
        else:
            logging.info(f"Process is not a recognized slicer: {exe_path}")
            return None, None
    except (psutil.AccessDenied, psutil.NoSuchProcess) as e:
        logging.warning(f"Could not access process executable: {e}")
        return None, None

def setup_realtime_plot():
    global plot_fig, plot_axes, plot_lines
    plot_fig, plot_axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    plot_fig.suptitle('Processing...', fontsize=14)
    plot_fig.canvas.manager.set_window_title("MZ Flow Temp processor")
    ax1 = plot_axes[0]
    plot_lines['original_flow'], = ax1.plot([0], [0], 'c-', alpha=0.5, label='Input Flow Rate')
    plot_lines['final_flow'], = ax1.plot([0], [0], 'b-', linewidth=2, label='Output Flow Rate')
    ax1.set_title('Flow Rate')
    ax1.set_xlabel('Time (s)')
    ax1.set_ylabel('Flow Rate (mm³/s)')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax2 = plot_axes[1]
    plot_lines['ideal_temp'], = ax2.plot([0], [0], 'g-', alpha=0.5, label='Ideal Temp for Output Flow')
    plot_lines['final_temp'], = ax2.plot([0], [0], 'b-', linewidth=2, label='Output Temperature')
    ax2.set_title('Temperature')
    ax2.set_xlabel('Time (s)')
    ax2.set_ylabel('Temperature (°C)')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.ion()
    plt.show(block=False)
    plot_fig.canvas.draw()
    plot_fig.canvas.flush_events()
    time.sleep(0.5)
    return plot_fig, plot_axes

def update_realtime_plot():
    global plotting_data, plot_lines, plot_axes, plot_fig
    if len(plotting_data['times']) == 0:
        return
    try:
        times = np.array(plotting_data['times'])
        flows = np.array(plotting_data['flows'])
        final_flows = np.array(plotting_data['final_flows'])
        final_temps = np.array(plotting_data['final_temps'])
        ideal_temps = np.array(plotting_data['ideal_temps'])
        plot_lines['original_flow'].set_data(times, flows)
        plot_lines['final_flow'].set_data(times, final_flows)
        plot_lines['final_temp'].set_data(times, final_temps)
        plot_lines['ideal_temp'].set_data(times, ideal_temps)
        for ax in plot_axes:
            ax.relim()
            ax.autoscale_view()
        if len(times) > 0 and len(flows) > 0:
            all_flows = np.concatenate([flows, final_flows])
            flow_min, flow_max = np.min(all_flows), np.max(all_flows)
            flow_range = flow_max - flow_min
            if flow_range > 0:
                margin = flow_range * 0.1
                plot_axes[0].set_ylim(flow_min - margin, flow_max + margin)
            all_temps = np.concatenate([final_temps, ideal_temps])
            temp_min, temp_max = np.min(all_temps), np.max(all_temps)
            temp_range = temp_max - temp_min
            if temp_range > 0:
                margin = max(5.0, temp_range * 0.1)
                plot_axes[1].set_ylim(temp_min - margin, temp_max + margin)
            time_min, time_max = np.min(times), np.max(times)
            time_range = time_max - time_min
            if time_range > 0:
                margin = time_range * 0.05
                for ax in plot_axes:
                    ax.set_xlim(0, time_max + margin)  # Always show from t=0 to latest
        plot_fig.canvas.draw_idle()
        plot_fig.canvas.flush_events()
        plt.pause(0.001)
    except Exception as e:
        logging.debug(f"Plot update error: {e}")

def add_data_point(time_val, flow, final_flow, final_temp, max_flow, ideal_temp):
    global plotting_data
    # Debug: Log the values being plotted
    logging.debug(f"add_data_point: time={time_val:.2f}, flow={flow:.3f}, final_flow={final_flow:.3f}, final_temp={final_temp:.2f}, max_flow={max_flow:.3f}, ideal_temp={ideal_temp:.2f}")
    plotting_data['times'].append(time_val)
    plotting_data['flows'].append(flow)
    plotting_data['final_flows'].append(final_flow)
    plotting_data['final_temps'].append(final_temp)
    plotting_data['ideal_temps'].append(ideal_temp)

def parse_gcode(filename):
    moves = []
    x = y = z = e = f = 0.0
    rel_e = False
    with open(filename, 'r', encoding='utf-8', errors='ignore') as fh:
        for i, line in enumerate(fh):
            if line.startswith('G90'):
                continue
            if line.startswith('G91'):
                continue
            if 'M83' in line:
                rel_e = True
            if 'M82' in line:
                rel_e = False
            m = RE_MOVE.search(line)
            if m:
                params = m.group(1)
                x1 = RE_X.search(params)
                y1 = RE_Y.search(params)
                z1 = RE_Z.search(params)
                e1 = RE_E.search(params)
                f1 = RE_F.search(params)
                x_new = x if not x1 else float(x1.group(1))
                y_new = y if not y1 else float(y1.group(1))
                z_new = z if not z1 else float(z1.group(1))
                if e1:
                    e_val = float(e1.group(1))
                    if rel_e:
                        e_new = e + e_val
                    else:
                        e_new = e_val
                else:
                    e_new = e
                f_new = f if not f1 else float(f1.group(1))
                extruding = (e_new - e) > 0.0
                moves.append(Move(x_new, y_new, z_new, e_new, f_new, extruding, i, line.rstrip()))
                x, y, z, e, f = x_new, y_new, z_new, e_new, f_new
    return moves

# Global settings dictionary
settings = {}

def parse_settings_from_gcode(filename):
    global settings
    # Separate print (slicer) settings and script-specific settings
    print_settings_keys = [
        'nozzle_temperature_range_high',
        'nozzle_temperature_range_low',
        'filament_diameter',
        'slow_down_min_speed',
        'filament_max_volumetric_speed',
        'nozzle_temperature_initial_layer',
        'initial_layer_print_height'
    ]
    script_settings_keys = [
        'mz_flow_temp_sec_per_c_heating',
        'mz_flow_temp_sec_per_c_cooling',
        'mz_flow_temp_launch_viewer'
    ]
    required_params = print_settings_keys + script_settings_keys

    print_settings = {k: None for k in print_settings_keys}
    script_settings = {k: None for k in script_settings_keys}

    logging.info("Looking for required settings in G-code comments and config blocks...")

    # Check for required block markers before parsing settings
    try:
        with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        has_exec_start = any('; EXECUTABLE_BLOCK_START' in line for line in lines)
        has_exec_end = any('; EXECUTABLE_BLOCK_END' in line for line in lines)
        has_mz_start = any('; MZ FLOW TEMP START' in line for line in lines)
        has_mz_end = any('; MZ FLOW TEMP END' in line for line in lines)
        if not (has_exec_start and has_exec_end):
            logging.error("ERROR: Missing ; EXECUTABLE_BLOCK_START or ; EXECUTABLE_BLOCK_END in G-code file.")
            sys.exit(3)
        if not (has_mz_start and has_mz_end):
            logging.error("ERROR: Missing ; MZ FLOW TEMP START or ; MZ FLOW TEMP END in G-code file.")
            sys.exit(4)
        # Now parse settings as before
        in_block = False
        for line in lines:
            if line.strip().startswith('; CONFIG_BLOCK_START'):
                in_block = True
                continue
            if line.strip().startswith('; CONFIG_BLOCK_END'):
                in_block = False
                continue
            # Parse config block lines (e.g., printer_notes)
            if in_block and 'printer_notes' in line and '=' in line:
                notes_content = line.split('=', 1)[1].strip()
                for note_line in notes_content.split('\\n'):
                    note_line = note_line.strip()
                    for key in script_settings_keys:
                        if note_line.startswith(f'{key} ='):
                            val = note_line.split('=', 1)[1].strip()
                            if key.endswith('launch_viewer'):
                                script_settings[key] = val.lower() == 'true'
                                logging.info(f"Found script setting in printer_notes: {key} = {script_settings[key]}")
                            else:
                                try:
                                    script_settings[key] = float(val)
                                    logging.info(f"Found script setting in printer_notes: {key} = {script_settings[key]}")
                                except ValueError:
                                    logging.warning(f"Could not parse value for {key}: {val}")
            # Parse regular settings as before (for print settings only)
            if line.startswith('; ') and ' = ' in line:
                content = line[2:].lstrip()
                if content:
                    for key in print_settings_keys:
                        if key not in content:
                            continue
                        parts = content.split('=', 1)
                        if len(parts) != 2:
                            continue
                        value_str = parts[1].strip().split(",")[0]
                        if value_str and (value_str[0].isdigit() or value_str[0] in '+-.'):
                            try:
                                print_settings[key] = float(value_str)
                                logging.info(f"Found setting in G-code: {key} = {print_settings[key]}")
                            except ValueError:
                                logging.warning(f"Could not parse value for {key}: {parts[1]}")
    except Exception as e:
        logging.error(f"Error parsing settings from G-code: {e}")
        sys.exit(5)

    # Merge for required param check
    merged_settings = {**print_settings, **script_settings}
    missing_params = [param for param in required_params if merged_settings[param] is None]
    if missing_params:
        logging.error("The following required parameters were not found in the G-code file:")
        for param in missing_params:
            logging.error(f"  - {param}")
        logging.error("Please ensure your G-code file contains these parameters in the header comments or config blocks.")
        logging.error("Example format: ; parameter_name = value")
        logging.error("Aborting processing.")
        sys.exit(6)
    # Merge for downstream use
    settings = {**print_settings, **script_settings}
    return settings

def process_moves_pressure_equalizer(moves):
    global settings
    # Use only settings for all configurable parameters
    area = filament_area(settings['filament_diameter'])
    smoothed_temp = settings['nozzle_temperature_range_low']
    global_time = 0.0
    last_update_time = 0.0
    LOOKAHEAD_TIME = 10.0

    # --- Vectorized calculation of move distances, times, and flows ---
    if len(moves) > 1:
        logging.info("Vectorized calculation of move distances, times, and flows...")
        x = np.array([m.x for m in moves])
        y = np.array([m.y for m in moves])
        z = np.array([m.z for m in moves])
        e = np.array([m.e for m in moves])
        f = np.array([m.f for m in moves])
        extruding = np.array([m.extruding for m in moves])

        # Calculate differences between moves
        dx = np.diff(x, prepend=x[0])
        dy = np.diff(y, prepend=y[0])
        dz = np.diff(z, prepend=z[0])
        de = np.diff(e, prepend=e[0])

        # Calculate move distances and times
        dist_xyz = np.sqrt(dx**2 + dy**2 + dz**2)
        dt = np.where(f > 0, dist_xyz / (f / 60.0), 0.0)
        global_times = np.cumsum(dt)

        # Assign calculated values back to moves
        for i, m in enumerate(moves):
            m.dist_xyz = dist_xyz[i]
            m.move_time = dt[i]
            m.global_time = global_times[i]

        # Calculate flow rates for extruding moves
        flow = np.zeros_like(e)
        valid = (extruding) & (dt > 0) & (de > 0)
        flow[valid] = (de[valid] * area) / dt[valid]
        for i, m in enumerate(moves):
            m.flow = flow[i]
            if m.extruding and flow[i] > 0:
                logging.debug(f"Move {i}: de={de[i]:.5f}, dt={dt[i]:.5f}, area={area:.5f}, flow={flow[i]:.3f}, F={m.f}")
    else:
        logging.info("Vectorized calculation skipped: not enough moves.")

    # --- Lookahead flow calculation for temperature smoothing ---
    logging.info("Calculating lookahead flows...")
    lookahead_flows = []
    for i, move in enumerate(moves):
        lookahead_flow = move.flow
        t0 = move.global_time
        t1 = t0 + LOOKAHEAD_TIME
        total_flow = 0.0
        total_time = 0.0
        for j in range(i, len(moves)):
            m2 = moves[j]
            if not m2.extruding or m2.global_time > t1:
                break
            dt = m2.move_time
            if dt > 0:
                total_flow += m2.flow * dt
                total_time += dt
        if total_time > 0:
            lookahead_flow = total_flow / total_time
        lookahead_flows.append(lookahead_flow)

    setup_realtime_plot()
    moves_with_time = []
    for i, move in enumerate(moves):
        if i > 0:
            moves_with_time.append((move, move.move_time, move.extruding, i))
    update_counter = 0
    plot_update_counter = 0
    first_update_done = False

    # --- Find the Z height of the first layer using initial_layer_print_height if available ---
    if settings.get('initial_layer_print_height') is not None:
        first_layer_z = settings['initial_layer_print_height']
        logging.info(f"Using initial_layer_print_height from G-code: {first_layer_z}")
    else:
        first_layer_z = None
        for m in moves:
            if m.extruding and m.flow > 0:
                first_layer_z = m.z
                logging.info(f"Using first extruding move Z as first layer: {first_layer_z}")
                break

    # --- Get initial layer temp from settings (from G-code header) ---
    initial_layer_temp = settings.get('nozzle_temperature_initial_layer', settings['nozzle_temperature_range_low'])

    # Collect flows from the first 30 extruding moves from the second layer
    second_layer_flows = []
    for m in moves:
        if m.extruding and m.flow > 0 and abs(m.z - first_layer_z) > 1e-5:
            second_layer_flows.append(m.flow)
            if len(second_layer_flows) >= 30:
                break
    if second_layer_flows:
        avg_second_flow = sum(second_layer_flows) / len(second_layer_flows)
        logging.info(f"Initial flow (second layer): {avg_second_flow:.3f} mm³/s")
        if settings['filament_max_volumetric_speed'] > 0:
            avg_second_temp = settings['nozzle_temperature_range_low'] + (
                (settings['nozzle_temperature_range_high'] - settings['nozzle_temperature_range_low']) *
                min(avg_second_flow, settings['filament_max_volumetric_speed']) / settings['filament_max_volumetric_speed']
            )
        else:
            avg_second_temp = settings['nozzle_temperature_range_low']
    else:
        avg_second_flow = 0.0
        avg_second_temp = settings['nozzle_temperature_range_low']

    smoothed_temp = avg_second_temp
    last_update_time = 0.0

    logging.info("Smoothing flows and adjusting temperature...")
    for idx, (move, dt, is_extruding, move_index) in enumerate(moves_with_time):
        global_time += dt
        if is_extruding:
            flow = move.flow
            if not hasattr(move, 'smoothed_flow') or move.smoothed_flow == 0.0:
                move.smoothed_flow = flow
            if not hasattr(move, 'final_flow') or move.final_flow == 0.0:
                move.final_flow = flow
        else:
            flow = 0.0

        # --- Set temp for first layer moves ---
        if abs(move.z - first_layer_z) < 1e-5:  # robust float comparison
            smoothed_temp = initial_layer_temp
            move.smoothed_temp = smoothed_temp
            move.max_allowed_flow = settings['filament_max_volumetric_speed']
        else:
            # Always adjust temperature (ADJUST_TEMP is always True)
            lookahead_flow = lookahead_flows[move_index] if move_index < len(lookahead_flows) else move.smoothed_flow
            if settings['filament_max_volumetric_speed'] > 0:
                target_flow = min(lookahead_flow, settings['filament_max_volumetric_speed'])
                target_temp = settings['nozzle_temperature_range_low'] + (
                    (settings['nozzle_temperature_range_high'] - settings['nozzle_temperature_range_low']) *
                    target_flow / settings['filament_max_volumetric_speed']
                )
            else:
                target_temp = settings['nozzle_temperature_range_low']
            elapsed = global_time - last_update_time
            if target_temp > smoothed_temp:
                sec_per_c = settings['mz_flow_temp_sec_per_c_heating']
            else:
                sec_per_c = settings['mz_flow_temp_sec_per_c_cooling']
            max_temp_change = elapsed / sec_per_c if sec_per_c > 1e-6 else elapsed
            temp_diff = target_temp - smoothed_temp
            if abs(temp_diff) > max_temp_change:
                smoothed_temp += math.copysign(max_temp_change, temp_diff)
                last_update_time = global_time
            elif abs(temp_diff) > 0:
                smoothed_temp = target_temp
                last_update_time = global_time
            smoothed_temp = max(settings['nozzle_temperature_range_low'],
                                min(smoothed_temp, settings['nozzle_temperature_range_high']))
            if settings['filament_max_volumetric_speed'] > 0 and (settings['nozzle_temperature_range_high'] - settings['nozzle_temperature_range_low']) > 1e-6:
                temp_ratio = (smoothed_temp - settings['nozzle_temperature_range_low']) / (settings['nozzle_temperature_range_high'] - settings['nozzle_temperature_range_low'])
                temp_ratio = max(0.0, temp_ratio)
                max_allowed_flow = settings['filament_max_volumetric_speed'] * temp_ratio
            else:
                max_allowed_flow = settings['filament_max_volumetric_speed']
            move.smoothed_temp = smoothed_temp
            move.max_allowed_flow = max_allowed_flow

        if is_extruding and flow > 0:
            final_flow = min(move.smoothed_flow, move.max_allowed_flow)
            move.final_flow = final_flow
            clamped_feedrate = move.f
            feedrate_was_clamped = False
            if flow > 0 and final_flow < flow:
                clamped_feedrate = (final_flow / flow) * move.f if flow > 0 else move.f
                clamped_feedrate = max(clamped_feedrate, settings['slow_down_min_speed'] * 60)
                feedrate_was_clamped = True
            move.clamped_feedrate = clamped_feedrate
            move.feedrate_was_clamped = feedrate_was_clamped
            update_counter += 1
            if update_counter % 100 == 0:
                if settings['filament_max_volumetric_speed'] > 0:
                    ideal_temp = settings['nozzle_temperature_range_low'] + (
                        (settings['nozzle_temperature_range_high'] - settings['nozzle_temperature_range_low']) *
                        min(final_flow, settings['filament_max_volumetric_speed']) / settings['filament_max_volumetric_speed']
                    )
                else:
                    ideal_temp = settings['nozzle_temperature_range_low']
                add_data_point(move.global_time, flow, final_flow, smoothed_temp, move.max_allowed_flow, ideal_temp)
            plot_update_counter += 1
            if (not first_update_done and plot_update_counter >= 100) or (plot_update_counter % 10000 == 0):
                update_realtime_plot()
                if not first_update_done:
                    first_update_done = True
        elif is_extruding:
            move.clamped_feedrate = move.f
            move.feedrate_was_clamped = False
    # Smooth final_flows before final plot
    times_np = np.array(plotting_data['times'])
    final_flows_np = np.array(plotting_data['final_flows'])
    smooth_window_time = max(settings['mz_flow_temp_sec_per_c_heating'], settings['mz_flow_temp_sec_per_c_cooling']) * 0.65
    plotting_data['final_flows'] = list(smooth_array(final_flows_np, times_np, window_sec=(smooth_window_time)))
    update_realtime_plot()
    logging.info("Updating plot and waiting for user to close window...")
    global plot_fig
    plot_fig.suptitle('Flow and Temperature', fontsize=14)
    plot_fig.canvas.draw()
    def on_key_press(event):
        # Set a flag if ESC is pressed, otherwise close as normal
        nonlocal esc_pressed
        if event.key in ['escape']:
            esc_pressed = True
            plt.close('all')
        elif event.key in ['q', 'Q']:
            esc_pressed = False
            plt.close('all')
    esc_pressed = False
    plot_fig.canvas.mpl_connect('key_press_event', on_key_press)
    plt.ioff()
    plt.show(block=True)
    return esc_pressed

def smooth_array(arr, times, window_sec=2.0):
    """
    Smooths the array using a moving average with a window of window_sec seconds.
    arr: numpy array of values to smooth
    times: numpy array of time values (same length as arr)
    window_sec: window size in seconds
    Returns: smoothed numpy array
    """
    import numpy as np
    if len(arr) < 2:
        return arr
    smoothed = np.zeros_like(arr)
    for i in range(len(arr)):
        t0 = times[i] - window_sec / 2
        t1 = times[i] + window_sec / 2
        mask = (times >= t0) & (times <= t1)
        if np.any(mask):
            smoothed[i] = np.mean(arr[mask])
        else:
            smoothed[i] = arr[i]
    return smoothed

# Add debug logging for G-code output feedrate and flow
def save_processed_gcode(filename, moves, mz_start=None, mz_end=None):
    logging.info(f"Saving processed G-code to {filename} ...")
    with open(filename, 'r', encoding='utf-8', errors='ignore') as fin:
        all_lines = fin.readlines()

    move_lookup = {move.line_num: move for move in moves if move.extruding}
    processed_lines = []
    last_temp_value = None

    for i, line in enumerate(all_lines):
        # Only process lines inside the MZ FLOW TEMP region within EXECUTABLE_BLOCK
        if i <= mz_start or i >= mz_end:
            processed_lines.append(line)
            continue

        if i in move_lookup:
            move = move_lookup[i]
            current_temp_int = int(round(move.smoothed_temp))
            if last_temp_value is None or current_temp_int != last_temp_value:
                processed_lines.append(f'M104 S{current_temp_int}\n')
                last_temp_value = current_temp_int
            if move.feedrate_was_clamped:
                m = RE_MOVE.search(line)
                if m:
                    line_wo_f = re.sub(r'\sF[-+]?\d*\.?\d+', '', line.rstrip())
                    parts = line_wo_f.split(';', 1)
                    gcode_part = parts[0].rstrip()
                    comment_part = ';' + parts[1] if len(parts) > 1 else ''
                    modified_line = f"{gcode_part} F{int(round(move.clamped_feedrate))} {comment_part}".rstrip() + '\n'
                    processed_lines.append(modified_line)
                    logging.debug(f"G-code line {i}: clamped_feedrate={move.clamped_feedrate:.2f}, final_flow={move.final_flow:.3f}, raw_flow={move.flow:.3f}")
                else:
                    processed_lines.append(line)
            else:
                processed_lines.append(line)
        else:
            processed_lines.append(line)

    try:
        with open(filename, 'w', encoding='utf-8', errors='ignore', newline='') as fout:
            fout.writelines(processed_lines)
            # Also save a copy for the viewer if enabled
            if settings.get('mz_flow_temp_launch_viewer', False):
                viewer_filename = os.path.join(os.path.dirname(log_path), "processed.gcode")
                try:
                    with open(viewer_filename, 'w', encoding='utf-8', errors='ignore', newline='') as backup_fout:
                        backup_fout.writelines(processed_lines)
                    logging.info(f"Viewer G-code saved to: {viewer_filename}")
                except Exception as e:
                    logging.warning(f"Could not save viewer G-code: {e}")
    except Exception as e:
        logging.error(f"ERROR writing file: {e}")
        sys.exit(8)
    logging.info("G-code saved successfully.")
    return filename

def get_marker_indices(filename):
    with open(filename, 'r', encoding='utf-8', errors='ignore') as fin:
        all_lines = fin.readlines()
    exec_start = next((i for i, l in enumerate(all_lines) if '; EXECUTABLE_BLOCK_START' in l), None)
    exec_end = next((i for i, l in enumerate(all_lines) if '; EXECUTABLE_BLOCK_END' in l), None)
    mz_start = next((i for i in range(exec_start, exec_end+1) if '; MZ FLOW TEMP START' in all_lines[i]), None)
    mz_end = next((i for i in range(exec_start, exec_end+1) if '; MZ FLOW TEMP END' in all_lines[i]), None)
    return mz_start, mz_end

def main():
    global settings
    logging.info("Welcome to MZ Flow Temp G-code post-processor")
    try:
        if len(sys.argv) < 2:
            logging.error('Usage: python mz_flow_temp.py <input.gcode>')
            sys.exit(1)
        filename = " ".join(sys.argv[1:]).strip().strip('"').strip("'")
        filename = os.path.normpath(filename)
        if not os.path.isfile(filename):
            logging.error(f'Input file not found: {filename!r}')
            sys.exit(2)
        logging.info(f'Input file: {filename}')
        parse_settings_from_gcode(filename)
        logging.info("Parsing G-code moves...")
        moves = parse_gcode(filename)
        if len(moves) == 0:
            logging.error("ERROR: No moves found!")
            sys.exit(7)

        # --- Only process moves between the markers ---
        mz_start, mz_end = get_marker_indices(filename)
        moves_in_region = [m for m in moves if mz_start < m.line_num < mz_end]

        if len(moves_in_region) == 0:
            logging.error("No moves found between MZ FLOW TEMP markers!")
            sys.exit(7)

        esc_pressed = process_moves_pressure_equalizer(moves_in_region)
        processed_filename = save_processed_gcode(filename, moves, mz_start, mz_end)
        if processed_filename:
            logging.info("Processing complete!")
        else:
            logging.error("Processing failed!")
            sys.exit(8)

        # Only launch viewer if not closed with ESC
        if settings.get('mz_flow_temp_launch_viewer', False) and not esc_pressed:
            _, parent_exe = get_parent_process_info()
            if parent_exe and os.path.isfile(parent_exe):
                parent_exe = os.path.normpath(parent_exe)
                viewer_filename = os.path.join(os.path.dirname(log_path), "processed.gcode")
                logging.info(f"Launching viewer process: {parent_exe} {viewer_filename}")
                try:
                    args = [str(parent_exe), str(viewer_filename)]
                    logging.debug(f"Launching viewer process with args: {args}")
                    proc = subprocess.Popen(args)
                    logging.debug(f"subprocess.Popen returned: {proc}")
                except Exception as e:
                    logging.warning(f"Failed to launch parent process: {e}")
            else:
                logging.warning("Could not determine parent process executable to relaunch.")
    except SystemExit:
        raise
    except Exception as e:
        logging.error(f"Unhandled exception: {e}")
        sys.exit(9)

if __name__ == '__main__':
    main()
