# 🔥 MZ Flow Temp Processor

Advanced G-code post-processor that intelligently smooths flow and adjusts temperature for superior 3D print quality and layer adhesion.

![Benchy temperature visualization](images/benchy_temp.png)
![Graphs](images/graphs.png)

## Features

- **🌊 Flow Smoothing**: Uses a duration-weighted lookahead window to predict upcoming flow demand and smooth feedrate transitions
- **🌡️ Dynamic Temperature Control**: Adjusts nozzle temperature ahead of actual flow changes - lookahead window is automatically sized to cover the full temperature range transition time
- **⚡ Feedrate Clamping**: Prevents exceeding printer's maximum volumetric flow capability
- **📊 Real-time Visualization**: Live plotting of flow and temperature profiles
- **🔗 Slicer Integration**: Works as post-processing script (OrcaSlicer tested)
- **⚙️ Automatic Configuration**: Reads parameters from G-code comments or config blocks
- **👀 Viewer Relaunch**: Optionally relaunches slicer viewer after processing

## Quick Start

### Installation

**Python 3 required with these packages:**
- `numpy`
- `matplotlib`
- `PyQt5`
- `psutil`

**Install dependencies:**

Ubuntu/Debian:
```bash
sudo apt install python3-matplotlib python3-numpy python3-pyqt5 python3-psutil
```

Arch/Cachyos:
```bash
sudo pacman -S python-matplotlib python-numpy python-pyqt5 python-psutil
```

Windows:
```cmd
pip install matplotlib numpy PyQt5 psutil
```

### Basic Usage

```bash
python mz_flow_temp.py <input.gcode>
```

## Debug Mode

By default, the script shows informational messages in terminal (if run manually). A log file is created only when an error occurs.

**Enable debug logging:**
Create an empty file called `DEBUG_ON` in the script folder.

This enables verbose debug output and creates a detailed log file for troubleshooting.

## OrcaSlicer Integration

### 1. Configure Machine G-code

Go to **Printer Settings > Machine G-code** and add:

- At the **end** of **Machine start G-code**: `; MZ FLOW TEMP START`
- At the **beginning** of **Machine end G-code**: `; MZ FLOW TEMP END`

![Machine G-code](images/start_end.png)

### 2. Add Script Settings

In **Printer Settings > Notes**, add:
```
mz_flow_temp_sec_per_c_heating = 6
mz_flow_temp_sec_per_c_cooling = 4
mz_flow_temp_launch_viewer = true
```

 **Note:** `sec_per_c_heating` and `sec_per_c_cooling` control how many seconds it takes the nozzle to change 1°C. These values also determine the lookahead window size - the script automatically looks far enough ahead to start temperature transitions in time. Larger values = slower transitions = larger lookahead window.

![Printer notes](images/printer_notes.png)

### 3. Configure Filament Settings

Set these values in your **Filament profile**:
- Temperature ranges (low/high)
- First layer temperature
- Maximum volumetric flow rate
- Minimum print speed

![Filament settings](images/low_high_temp.png) ![First layer temp](images/first_layer_temp.png) ![Max flow rate](images/max_vfr.png) ![Min print speed](images/min_print_speed.png)

### 4. Add Post-processing Script

In **Print process > Others > Post-processing scripts**:

**Linux/MacOS:**
```bash
python <path to script>/mz_flow_temp.py
```

**Windows:**
1. Find Python path: `where python`
2. Add full command: `"C:\Users\<USER>\AppData\Local\Programs\Python\Python313\python.exe" <path to script>/mz_flow_temp.py`

![Post script](images/post_script.png)

## Viewer Behavior

**Important:** OrcaSlicer doesn't automatically show post-processed G-code. The script can relaunch the viewer to display changes.

- **ESC key**: Close plot without launching viewer
- **Window close/Q key**: Close plot and launch viewer (if enabled)

Disable viewer: `mz_flow_temp_launch_viewer = false`

## Required Parameters

Your G-code must include:

```gcode
; nozzle_temperature_range_high = 260
; nozzle_temperature_range_low = 220
; filament_diameter = 1.75
; slow_down_min_speed = 30
; filament_max_volumetric_speed = 12
; nozzle_temperature_initial_layer = 240
; initial_layer_print_height = 0.2
```

Plus in printer notes:
```
mz_flow_temp_sec_per_c_heating = 6
mz_flow_temp_sec_per_c_cooling = 4
mz_flow_temp_launch_viewer = true
```

## How It Works

1. **Parse G-code**: Extract all moves and calculate XYZ distances, move times, and per-move flow rates
2. **Lookahead**: For each move, compute a duration-weighted average flow over the upcoming window (sized to cover the full temp range transition). Travel moves are excluded from the average
3. **Adjust Temperature**: Track a smoothed temperature that ramps toward the lookahead target at the configured `sec_per_c` rate, starting from `nozzle_temperature_range_low`
4. **Clamp Feedrate**: Where lookahead flow exceeds what the current temperature can support, reduce feedrate proportionally - with a minimum speed floor and short-move exemption
5. **Visualize**: Display input flow, output flow, ideal temperature, and actual output temperature in real time
6. **Save & Launch**: Write M104 temperature commands and adjusted feedrates into the G-code, optionally relaunch slicer viewer

## Expected Results

Temperature changes are predicted ahead of time based on upcoming flow demand, so the nozzle is already at the right temperature when it needs to be - not catching up after the fact. Feedrate is reduced when the nozzle is too cold to safely handle the requested flow, preventing over-extrusion and pressure spikes. Most effective on prints with large infill-to-perimeter transitions such as the Benchy hull line.

## Exit Codes

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | Incorrect usage |
| 2 | Input file not found |
| 3 | Missing EXECUTABLE_BLOCK markers |
| 4 | Missing MZ FLOW TEMP markers |
| 5 | Error parsing settings |
| 6 | Missing required parameters |
| 7 | No moves found in G-code |
| 8 | Error writing output file |
| 9 | Unhandled exception |

## Troubleshooting

 - **No plots/output**: Check dependencies and verify G-code contains all required parameters and markers
 - **Script not running**: Verify post-processing script path and Python executable permissions
 - **Temperature not changing enough**: Your `nozzle_temperature_range_high` and `nozzle_temperature_range_low` may be too close together - widen the range in your filament profile
 - **Temperature reacting too slowly**: Decrease `sec_per_c_heating` / `sec_per_c_cooling` values
 - **Temperature reacting too aggressively**: Increase `sec_per_c_heating` / `sec_per_c_cooling` values
 - **Incorrect adjustments**: Ensure `filament_max_volumetric_speed` is accurately calibrated for your filament - an incorrect value skews the entire flow-to-temperature mapping

## License

GPL v3

## Support

[![Donate](https://storage.ko-fi.com/cdn/fullLogoKofi.png)](https://ko-fi.com/yurymonzon)

If this project helps you, consider supporting my work: https://ko-fi.com/yurymonzon

## Author

Developed by Yury MonZon, inspired by BELAYEL Salim's original temperature controller: https://github.com/sb53systems/G-Code-Flow-Temperature-Controller
