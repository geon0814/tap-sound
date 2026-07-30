# tap-sound
**Explore the hidden sensors inside Apple Silicon MacBooks** with a lightweight Tkinter desktop app powered by the [`macimu`](https://pypi.org/project/macimu/) IOKit/HID bridge. It exposes hidden hardware sensors—including the accelerometer, lid angle, ambient light sensor, battery telemetry, Wi-Fi, thermal state, and power usage—and transforms them into interactive features such as tap-to-sound, parking-sensor lid warnings, synchronized haptic feedback, and keyboard backlight effects.

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
  > **Known limitation**: macOS only fires the Taptic Engine while it detects a finger
  > on the trackpad (capacitive touch) - this is by design, so the haptic only helps
  > when a hand is resting on the trackpad.
- **Keyboard backlight flash** - while the warning is active, the keyboard backlight
  flashes in sync with each beep via `KeyboardBrightnessClient` (raw ObjC runtime call,
  `CoreBrightness.framework`), then restores the original brightness once the lid
  returns to a safe angle.
- **Ambient light sensor** - real-time lux reading.
- **Battery** - percentage, temperature, voltage/current, charging state, cycle count
  (via `ioreg`).
- **Wi-Fi** - SSID, RSSI, channel (via `wdutil info`).
- **CPU / GPU / ANE power** - live Watts via `powermetrics`.
- **Thermal state** - `NSProcessInfo.thermalState` via raw ObjC runtime calls.
- **Trackpad contact bar** - real-time color bar (blue → red) showing contact intensity
  via `MultitouchSupport.framework` (no root required). See implementation notes below.

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

---

## Implementation notes: trackpad contact bar

### How it works

The trackpad bar reads raw multitouch data from Apple's private
`MultitouchSupport.framework` via ctypes, without using pyobjc or NSEvent.
This approach works even as root (unlike NSEvent global monitors) and requires
no Accessibility permissions.

```python
_mt = ctypes.cdll.LoadLibrary(
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"
)
```

Each frame, the framework calls a registered callback with a pointer to an array
of `MTContact` structs (one per finger). The struct is 96 bytes (stride established
from community reverse-engineering documentation). The value at **offset 92** is
read as a `float` and used to drive the bar.

### Why NSEvent was abandoned

Two separate failures:

1. **pyobjc + Tkinter conflict** — importing `AppKit` at module level causes pyobjc
   to initialize `NSApplication` before Tkinter's own subclass is ready, triggering
   `-[NSApplication macOSVersion]: unrecognized selector` during `Tk_Init`.

2. **Root process has no window server session** — `NSEvent` global monitors receive
   no events when the process runs as root. macOS routes HID events only to processes
   with an active GUI session, which a `sudo`-launched terminal process does not have.

### Finding offset 92

The MTContact struct layout was found empirically by printing every 4-byte float
across the 96-byte struct while touching the trackpad:

```
offset 32: 0.573   ← normalized x position
offset 36: 0.651   ← normalized y position
offset 56: 1.571   ← angle (≈ π/2 radians)
offset 92: 1.172   ← this field
```

Offset 92 changed consistently with contact, so it was chosen for the bar.

**Caveat:** the stride (96 bytes) came from prior community documentation, not from
independent measurement. A proper verification would put two fingers on the trackpad,
scan offsets 0–200, and confirm the second finger's x/y values reappear at exactly
+96. This has not been done — `range(0, CONTACT_SIZE, 4)` in the debug loop already
assumed 96, making "the last offset is 92" circular, not an independent check.

### What offset 92 actually measures

Empirical results on MacBook Air M4 (collected by printing session-peak values):

| Interaction | raw (offset 92) | size (offset 48) | ratio |
|---|---|---|---|
| Light finger touch | 1.61 | 0.71 | 2.28 |
| Normal click | 1.58 | 0.75 | 2.12 |
| Force Click | 1.54 | 0.80 | 1.91 |
| Palm (centered) | 4.50 | 45.13 | 0.10 |

Two findings contradict each other's naive interpretation:

- **Finding 1**: within finger interactions, raw *decreases* slightly as force increases
  (touch > click > Force Click). Difference is within noise (~0.04–0.07), so click
  stage is undetectable from this field. This rules out "raw encodes pressing force."

- **Finding 2**: palm raw is only 3× finger raw, but palm size is 56× finger size.
  If raw were total force, it should be independent of area. It isn't.
  This rules out "raw encodes total force."

Both assumptions fail simultaneously, so the field is neither pressure (force/area)
nor total force. The best current hypothesis is that it is an **internal contact
geometry metric**, possibly used for palm rejection — the ratio (raw/size) separates
fingers (~2.0) from palms (~0.1) cleanly, which is exactly the discriminant you'd
want for that purpose.

**Practical consequence:** the color bar is valid as a "contact presence + palm
detection" indicator. Using this field for Force Click stage detection is not viable.
