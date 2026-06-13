# tap-sound

A little Tkinter desktop app that taps into the hidden sensors of Apple Silicon MacBooks
(via the [`macimu`](https://pypi.org/project/macimu/) IOKit/HID bridge) and turns them into
something fun and useful.

## Features

- **Tap detection** - taps/hits on the laptop body (via the accelerometer) play a random
  macOS system sound.
- **Lid angle** - reads the hinge angle sensor in real time.
- **Parking-sensor warning beep** - as the lid angle approaches the hinge's mechanical
  limit, an audible beep speeds up smoothly (exponential curve), just like a car's
  reversing sensor. The thresholds (`LID_DANGER_START` / `LID_DANGER_MAX`) were tuned for a
  **MacBook Air M4 13"** and may need adjusting on other models.
- **Trackpad haptic feedback** - each warning beep is paired with a short haptic tap via
  `NSHapticFeedbackManager` (raw ObjC runtime call) on Force Touch trackpads.
- **Ambient light sensor** - real-time lux reading.
- **Battery** - percentage, temperature, voltage/current, charging state, cycle count
  (via `ioreg`).
- **Wi-Fi** - SSID, RSSI, channel (via `wdutil info`).
- **CPU / GPU / ANE power** - live Watts via `powermetrics`.
- **Thermal state** - `NSProcessInfo.thermalState` via raw ObjC runtime calls.

## Requirements

- Apple Silicon Mac
- Python 3 (developed on 3.14)
- `macimu` (pip)

## Setup

```bash
python3 -m venv venv
venv/bin/pip install macimu
```

## Run

Sensor access (accelerometer, lid angle, ALS, Wi-Fi info, powermetrics) requires root:

```bash
sudo venv/bin/python3 sensor_ui.py
```

A simpler CLI-only tap-to-sound demo is available too:

```bash
sudo venv/bin/python3 tap_sound.py [threshold]
```
