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

## Technologies

- IOKit / HID (via `macimu`)
- Objective-C Runtime (raw ctypes calls — no pyobjc)
- `CoreBrightness.framework` (keyboard backlight)
- `MultitouchSupport.framework` (private, trackpad multitouch data)
- `ioreg` / `wdutil` / `powermetrics` (system CLI tools)

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

## Known Limitations

- macOS only fires the Taptic Engine while it detects a finger on the trackpad
  (capacitive touch). Haptic feedback during lid warnings only works when a hand
  is resting on the trackpad.
- Lid angle thresholds were tuned on a MacBook Air M4 13" and may need adjusting
  on other models.
- This project relies on undocumented Apple private frameworks
  (`MultitouchSupport`, `CoreBrightness`). Future macOS releases may change or
  remove these interfaces without notice.

---

## Implementation notes: trackpad contact bar

### Summary

A color bar (blue → red) driven by raw multitouch data from
`MultitouchSupport.framework`. Responds visibly to contact presence and palm
placement; not suitable for distinguishing click stages. See experimental results
below for why.

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

NSEvent (`event.pressure()` / `event.stage()`) was the first approach attempted
and failed for two independent reasons — see below.

### Reverse-engineering the struct

The MTContact struct layout was found empirically by printing every 4-byte float
across the 96-byte struct while touching the trackpad:

```
offset 32: 0.573   ← normalized x position
offset 36: 0.651   ← normalized y position
offset 56: 1.571   ← angle (≈ π/2 radians)
offset 92: 1.172   ← this field
```

Offset 92 changed consistently with contact, so it was chosen for the bar.

**Caveat on the stride:** the 96-byte stride came from prior community documentation,
not independent measurement. The debug loop used `range(0, CONTACT_SIZE, 4)` with
`CONTACT_SIZE = 96` already set, so "the last offset is 92" is circular — it follows
from the assumed stride, not the other way around. A proper verification would scan
offsets 0–200 with two fingers and confirm the second finger's x/y reappears at
exactly +96.

**Why NSEvent was abandoned:**

1. **pyobjc + Tkinter conflict** — importing `AppKit` at module level causes pyobjc
   to initialize `NSApplication` before Tkinter's own subclass is ready, triggering
   `-[NSApplication macOSVersion]: unrecognized selector` during `Tk_Init`.

2. **Root process has no window server session** — `NSEvent` global monitors receive
   no events when the process runs as root. macOS routes HID events only to processes
   with an active GUI session, which a `sudo`-launched terminal process does not have.

### Experimental results

87 session-peak measurements collected on MacBook Air M4 across 19 contact
categories (offset 92 and offset 48 simultaneously). Each row is the within-session
maximum for the contact with the highest raw value.

| Contact type | n | avg size | avg ratio |
|---|---|---|---|
| Feather touch | 2 | 0.26 | 2.38 |
| Fingernail | 1 | 0.23 | 2.36 |
| Fingertip vertical (all 4 fingers) | 11 | 0.50 | 2.35 |
| Resting touch | 2 | 0.52 | 2.28 |
| Force Click haptic artifact | 2 | 0.09 | 2.38 |
| Light click | 2 | 0.73 | 2.04 |
| Normal click | 5 | 0.74 | 2.13 |
| Hard click | 2 | 0.79 | 1.89 |
| Force Click (slow / sustained) | 12 | 1.01 | 1.59 |
| Corner / edge touches | 12 | 0.78 | 1.73 |
| 2-finger adjacent | 5 | 0.92 | 1.26 |
| Index pad flat | 7 | 1.19 | 0.97 |
| Thumb pad flat | 7 | 2.07 | 0.65 |
| Finger side ~45° | 4 | 2.36 | 0.50 |
| Finger side lengthwise | 9 | 3.70 | 0.42 |
| Finger full side flat | 2 | 10.89 | 0.24 |
| Hand edge (karate chop) | 1 | 14.11 | 0.21 |
| Palm partial (heel) | 2 | 24.56 | 0.12 |
| Palm full | 4 | 35.69 | 0.13 |

**Overall trend:** from feather touch (size 0.26, ratio 2.38) to full palm (size 35.7,
ratio 0.13), contact area spans 100× while ratio decreases monotonically. The
relationship is smooth enough that ratio alone gives a coarse but consistent encoding
of contact geometry across all 87 measurements.

**Force Click haptic artifact:** the two near-zero rows (raw ≈ 0.22, size ≈ 0.09,
ratio ≈ 2.38) previously seemed anomalous. With the full dataset they fall exactly
on the trend line — they are ordinary "barely-there contact" readings, not a field
anomaly. The artifact arises because a fast Force Click causes the trackpad's haptic
engine to briefly interrupt capacitive contact, ending the session at the initial
light-press frame before the heavier press is recorded. Slow or sustained Force Click
avoids this and gives size ≈ 1.0, ratio ≈ 1.6.

**Finger independence:** all four fingers (index, middle, ring, thumb) give ratio
2.33–2.36 for vertical fingertip contact. The field encodes contact geometry, not
which finger is used.

**Click stage detection:** click, hard click, and Force Click overlap heavily in both
raw and ratio. The field cannot distinguish them reliably.

### Known bug: multi-finger touches are not aggregated

The analysis code selects only the contact with the highest raw value per frame — it
does not sum or average across simultaneous contacts. As a result, the "two-finger"
and "three-finger" categories in the table above do not represent combined contact
area; they reflect whichever single finger had the highest raw value at each frame.

This was discovered after an unexplained paradox (2-finger sessions showing
*larger* average size than 3-finger sessions) led to a code review. What is confirmed:
the max-only selection. What is **not** confirmed: the cause of the paradox. Two
candidate explanations — capacitive crosstalk between adjacent contacts inflating the
detected size of neighboring fingers, vs. inconsistent finger selection (different
fingers dominating in each test condition) — cannot be distinguished with the current
data. The "손가락 개수" (finger-count) categories should be treated as unreliable.

### Limitations

The field at offset 92 is neither pressure (force/area) nor total force. The best
current hypothesis is an **internal contact geometry metric** used for palm rejection —
the ratio (raw/size) monotonically separates fingertip (~2.3) from palm (~0.1) across
87 measurements, which is exactly the discriminant needed for that purpose.

The color bar is a valid indicator of contact presence and palm placement. Force Click
stage detection from this field is not viable.
