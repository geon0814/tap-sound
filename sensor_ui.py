"""
MacBook Sensor Monitor - plays a sound on tap + shows lid angle / ambient light in real time
Run: sudo venv/bin/python3 sensor_ui.py
"""

import ctypes
import math
import plistlib
import random
import re
import subprocess
import threading
import tkinter as tk

from macimu import IMU

SOUNDS = [
    "/System/Library/Sounds/Pop.aiff",
    "/System/Library/Sounds/Tink.aiff",
    "/System/Library/Sounds/Bottle.aiff",
    "/System/Library/Sounds/Frog.aiff",
    "/System/Library/Sounds/Funk.aiff",
    "/System/Library/Sounds/Sosumi.aiff",
]

POLL_MS = 30          # UI refresh interval (ms)
SLOW_POLL_MS = 2000   # battery/Wi-Fi/thermal refresh interval (ms) - no need to call often
COOLDOWN_MS = 250     # minimum wait before the next tap can trigger (ms)

# Lid-angle warning beep (parking-sensor style - beeps faster near the hinge limit)
# NOTE: these angle thresholds were tuned on a MacBook Air M4 13" - the actual
# hinge limit may vary on other models.
LID_DANGER_START = 125.0  # beeping starts at this angle (near the hinge limit)
LID_DANGER_MAX = 132.0    # fastest beep at/above this angle (needs force to open further - danger!)
LID_BEEP_SLOW_MS = 1000
LID_BEEP_FAST_MS = 80
LID_BEEP_SOUND = "/System/Library/Sounds/Tink.aiff"


def lid_beep_interval(angle):
    """Beep interval (ms) for the given lid angle, or None if within the safe range.

    The interval shrinks by a constant ratio per degree (exponential curve),
    giving a smooth, parking-sensor-like acceleration.
    """
    if angle < LID_DANGER_START:
        return None
    if angle >= LID_DANGER_MAX:
        return LID_BEEP_FAST_MS
    ratio = (angle - LID_DANGER_START) / (LID_DANGER_MAX - LID_DANGER_START)
    return LID_BEEP_SLOW_MS * (LID_BEEP_FAST_MS / LID_BEEP_SLOW_MS) ** ratio

# --- NSProcessInfo.thermalState (raw ObjC runtime calls, no sudo/extra deps needed) ---
_objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")

_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]

_msg_send_ptr = ctypes.cast(_objc.objc_msgSend, ctypes.c_void_p).value
_send_obj = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(_msg_send_ptr)
_send_int = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(_msg_send_ptr)

_NSProcessInfo = _objc.objc_getClass(b"NSProcessInfo")
_sel_processInfo = _objc.sel_registerName(b"processInfo")
_sel_thermalState = _objc.sel_registerName(b"thermalState")

# NSProcessInfoThermalState: 0=Nominal, 1=Fair, 2=Serious, 3=Critical
_THERMAL_STATES = {0: "Nominal", 1: "Fair", 2: "Serious", 3: "Critical (throttling)"}


def read_thermal_state():
    info = _send_obj(_NSProcessInfo, _sel_processInfo)
    state = _send_int(info, _sel_thermalState)
    return _THERMAL_STATES.get(state, f"Unknown({state})")


# --- NSHapticFeedbackManager (Force Touch trackpad haptic, raw ObjC runtime call) ---
ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")

_send_void_ll = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_long
)(_msg_send_ptr)

_NSHapticFeedbackManager = _objc.objc_getClass(b"NSHapticFeedbackManager")
_sel_defaultPerformer = _objc.sel_registerName(b"defaultPerformer")
_sel_performFeedback = _objc.sel_registerName(b"performFeedbackPattern:performanceTime:")

NS_HAPTIC_PATTERN_GENERIC = 0
NS_HAPTIC_PERFORMANCE_TIME_NOW = 1


def trigger_haptic():
    """Fire a short haptic tap on the Force Touch trackpad."""
    performer = _send_obj(_NSHapticFeedbackManager, _sel_defaultPerformer)
    _send_void_ll(performer, _sel_performFeedback, NS_HAPTIC_PATTERN_GENERIC, NS_HAPTIC_PERFORMANCE_TIME_NOW)


# --- KeyboardBrightnessClient (keyboard backlight, raw ObjC runtime call) ---
ctypes.cdll.LoadLibrary("/System/Library/PrivateFrameworks/CoreBrightness.framework/CoreBrightness")

_sel_alloc = _objc.sel_registerName(b"alloc")
_sel_init = _objc.sel_registerName(b"init")
_send_alloc_init = lambda cls: _send_obj(_send_obj(cls, _sel_alloc), _sel_init)

_send_float_int = ctypes.CFUNCTYPE(
    ctypes.c_float, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int
)(_msg_send_ptr)
_send_void_float_int = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_int
)(_msg_send_ptr)

_KeyboardBrightnessClient = _objc.objc_getClass(b"KeyboardBrightnessClient")
_sel_getKeyboardBrightness = _objc.sel_registerName(b"brightnessForKeyboard:")
_sel_setKeyboardBrightness = _objc.sel_registerName(b"setBrightness:forKeyboard:")
_kbd_brightness_client = _send_alloc_init(_KeyboardBrightnessClient)

KEYBOARD_BACKLIGHT_ID = 2  # the internal keyboard, found by probing IDs 0-4


def read_keyboard_brightness():
    """Current keyboard backlight brightness (0.0-1.0)."""
    return _send_float_int(_kbd_brightness_client, _sel_getKeyboardBrightness, KEYBOARD_BACKLIGHT_ID)


def set_keyboard_brightness(value):
    """Set the keyboard backlight brightness (0.0-1.0)."""
    _send_void_float_int(_kbd_brightness_client, _sel_setKeyboardBrightness, value, KEYBOARD_BACKLIGHT_ID)


def play_random_sound():
    subprocess.Popen(
        ["afplay", random.choice(SOUNDS)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def read_battery():
    """Read battery info from ioreg. (Temperature is in 1/100 deg C, Amperage in mA)"""
    out = subprocess.run(
        ["ioreg", "-r", "-c", "AppleSmartBattery", "-a"],
        capture_output=True, check=True,
    ).stdout
    items = plistlib.loads(out)
    if not items:
        return None
    data = items[0]
    return {
        "temperature": data["Temperature"] / 100.0,
        "voltage": data["Voltage"] / 1000.0,
        "amperage": data["Amperage"],
        "percent": data["CurrentCapacity"],
        "cycle_count": data["CycleCount"],
        "is_charging": data["IsCharging"],
        # "Maximum Capacity" as shown in System Settings > Battery > Battery Health
        "max_capacity_pct": data["AppleRawMaxCapacity"] / data["DesignCapacity"] * 100.0,
    }


def read_wifi():
    """Read Wi-Fi RSSI/channel etc. from wdutil info. (values are populated only when running as root)"""
    out = subprocess.run(
        ["wdutil", "info"], capture_output=True, text=True,
    ).stdout
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("RSSI", "Noise", "Channel", "SSID", "Tx Rate"):
            info.setdefault(key, value.strip())
    return info


class PowerMonitor:
    """Continuously runs powermetrics in the background to track CPU/GPU/ANE power (W)."""

    # optional "E-" / "P-" prefix handles Apple Silicon per-cluster lines
    _POWER_RE = re.compile(r"^(?:[A-Z]-)?(CPU|GPU|ANE) Power:\s*(\d+)\s*mW")
    _SAMPLE_RE = re.compile(r"^\*{3} Sampled")

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"cpu_W": 0.0, "gpu_W": 0.0, "ane_W": 0.0}
        self._proc = subprocess.Popen(
            ["powermetrics", "-i", "1000", "-s", "cpu_power"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        cpu_acc = 0.0
        for line in self._proc.stdout:
            line = line.strip()
            if self._SAMPLE_RE.match(line):
                with self._lock:
                    self._data["cpu_W"] = cpu_acc
                cpu_acc = 0.0
                continue
            m = self._POWER_RE.match(line)
            if not m:
                continue
            kind, mw = m.group(1), int(m.group(2)) / 1000.0
            if kind == "CPU":
                cpu_acc += mw
            else:
                key = "gpu_W" if kind == "GPU" else "ane_W"
                with self._lock:
                    self._data[key] = mw

    def read(self):
        with self._lock:
            return dict(self._data)

    def stop(self):
        self._proc.terminate()


class SensorUI(tk.Tk):
    def __init__(self, imu: IMU):
        super().__init__()
        self.imu = imu
        self.title("MacBook Sensor Monitor")

        self.threshold = tk.DoubleVar(value=0.2)
        self.prev_mag = None
        self.tap_count = 0
        self.cooldown_left = 0
        self.last_lid_angle = None
        self.lid_beep_cooldown = 0
        self.kbd_brightness_saved = None
        self.kbd_flash_on = False
        self.power = PowerMonitor()

        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()
        self._poll_slow()

    def _on_close(self):
        self.power.stop()
        self.destroy()

    def _build_widgets(self):
        pad = {"padx": 16, "pady": 6}

        tk.Label(self, text="Acceleration delta (g)", font=("Helvetica", 12)).pack(**pad)
        self.delta_label = tk.Label(self, text="0.000", font=("Helvetica", 28))
        self.delta_label.pack()

        tk.Label(self, text="Threshold - a change larger than this is detected as a 'tap'").pack()
        tk.Scale(self, from_=0.05, to=1.0, resolution=0.01,
                 orient="horizontal", length=260,
                 variable=self.threshold).pack(**pad)

        self.tap_label = tk.Label(self, text="Tap! 0", font=("Helvetica", 16), fg="gray")
        self.tap_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        self.lid_label = tk.Label(self, text="Lid angle: measuring...", font=("Helvetica", 14))
        self.lid_label.pack(**pad)

        self.als_label = tk.Label(self, text="Light: measuring...", font=("Helvetica", 14))
        self.als_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        self.battery_label = tk.Label(self, text="Battery: measuring...", font=("Helvetica", 14))
        self.battery_label.pack(**pad)

        self.wifi_label = tk.Label(self, text="Wi-Fi: measuring...", font=("Helvetica", 14))
        self.wifi_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        self.power_label = tk.Label(self, text="Power: measuring...", font=("Helvetica", 14))
        self.power_label.pack(**pad)

        self.thermal_label = tk.Label(self, text="Thermal state: -", font=("Helvetica", 14))
        self.thermal_label.pack(**pad)

    def _poll(self):
        # Acceleration delta -> tap detection
        for s in self.imu.read_accel():
            mag = math.sqrt(s.x ** 2 + s.y ** 2 + s.z ** 2)
            if self.prev_mag is not None:
                delta = abs(mag - self.prev_mag)
                self.delta_label.config(text=f"{delta:.3f}")

                if delta > self.threshold.get() and self.cooldown_left <= 0:
                    self.tap_count += 1
                    self.tap_label.config(text=f"Tap! {self.tap_count}", fg="red")
                    play_random_sound()
                    self.cooldown_left = COOLDOWN_MS
                    self.after(150, lambda: self.tap_label.config(fg="gray"))
            self.prev_mag = mag

        if self.cooldown_left > 0:
            self.cooldown_left -= POLL_MS

        # Lid angle (a new value only arrives when it changes)
        lid = self.imu.read_lid()
        if lid is not None:
            self.last_lid_angle = lid

        if self.last_lid_angle is not None:
            angle = self.last_lid_angle
            interval = lid_beep_interval(angle)
            if interval is not None:
                if self.kbd_brightness_saved is None:
                    self.kbd_brightness_saved = read_keyboard_brightness()

                warn = "Danger! Stop opening" if angle >= LID_DANGER_MAX else "Caution: near limit"
                self.lid_label.config(text=f"Lid angle: {angle:.1f}°  -  {warn}", fg="red")
                self.lid_beep_cooldown -= POLL_MS
                if self.lid_beep_cooldown <= 0:
                    subprocess.Popen(
                        ["afplay", LID_BEEP_SOUND],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    trigger_haptic()
                    self.kbd_flash_on = not self.kbd_flash_on
                    set_keyboard_brightness(1.0 if self.kbd_flash_on else 0.0)
                    self.lid_beep_cooldown = interval
            else:
                self.lid_label.config(text=f"Lid angle: {angle:.1f}°", fg="black")
                self.lid_beep_cooldown = 0
                if self.kbd_brightness_saved is not None:
                    set_keyboard_brightness(self.kbd_brightness_saved)
                    self.kbd_brightness_saved = None

        # Ambient light (a new value only arrives when it changes)
        als = self.imu.read_als()
        if als is not None:
            self.als_label.config(text=f"Light: {als.lux:.0f} lux")

        # CPU/GPU/ANE power (continuously updated by the background powermetrics thread)
        p = self.power.read()
        cpu_str = f"{p['cpu_W']:.2f}W (N/A macOS 26b bug)" if p['cpu_W'] == 0.0 else f"{p['cpu_W']:.2f}W"
        self.power_label.config(
            text=f"Power: CPU {cpu_str}  /  GPU {p['gpu_W']:.2f}W  /  ANE {p['ane_W']:.2f}W"
        )

        self.after(POLL_MS, self._poll)

    def _poll_slow(self):
        # Battery
        battery = read_battery()
        if battery is not None:
            state = "Charging" if battery["is_charging"] else "Discharging"
            self.battery_label.config(
                text=(
                    f"Battery: {battery['percent']}%  |  {battery['temperature']:.1f}°C  |  "
                    f"{battery['amperage']:+d}mA ({state})  |  Cycle {battery['cycle_count']}  |  "
                    f"Max capacity {battery['max_capacity_pct']:.1f}%"
                )
            )

        # Wi-Fi
        wifi = read_wifi()
        if wifi:
            self.wifi_label.config(
                text=(
                    f"Wi-Fi: {wifi.get('SSID', '-')}  |  RSSI {wifi.get('RSSI', '-')}  |  "
                    f"Channel {wifi.get('Channel', '-')}"
                )
            )

        # Thermal state
        self.thermal_label.config(text=f"Thermal state: {read_thermal_state()}")

        self.after(SLOW_POLL_MS, self._poll_slow)


def main():
    with IMU(accel=True, gyro=False, als=True, lid=True, sample_rate=100) as imu:
        SensorUI(imu).mainloop()


if __name__ == "__main__":
    main()
