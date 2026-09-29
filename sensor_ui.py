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
SLOW_POLL_MS = 2000   # battery/Wi-Fi/thermal refresh interval (ms)
COOLDOWN_MS = 250     # minimum wait before the next tap can trigger (ms)

LID_DANGER_START = 125.0
LID_DANGER_MAX = 132.0
LID_BEEP_SLOW_MS = 1000
LID_BEEP_FAST_MS = 80
LID_BEEP_SOUND = "/System/Library/Sounds/Tink.aiff"

# Trackpad state — written by the MultitouchSupport callback (background thread),
# read by _poll (main thread). "contacts" is replaced as a whole list each frame.
_pressure = {"value": 0.0, "contacts": []}


def lid_beep_interval(angle):
    if angle < LID_DANGER_START:
        return None
    if angle >= LID_DANGER_MAX:
        return LID_BEEP_FAST_MS
    ratio = (angle - LID_DANGER_START) / (LID_DANGER_MAX - LID_DANGER_START)
    return LID_BEEP_SLOW_MS * (LID_BEEP_FAST_MS / LID_BEEP_SLOW_MS) ** ratio


# --- NSProcessInfo.thermalState ---
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

_THERMAL_STATES = {0: "Nominal", 1: "Fair", 2: "Serious", 3: "Critical (throttling)"}


def read_thermal_state():
    info = _send_obj(_NSProcessInfo, _sel_processInfo)
    state = _send_int(info, _sel_thermalState)
    return _THERMAL_STATES.get(state, f"Unknown({state})")


# --- NSHapticFeedbackManager ---
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
    performer = _send_obj(_NSHapticFeedbackManager, _sel_defaultPerformer)
    _send_void_ll(performer, _sel_performFeedback, NS_HAPTIC_PATTERN_GENERIC, NS_HAPTIC_PERFORMANCE_TIME_NOW)


# --- HID temperature sensors (battery / SoC die / NAND) ---
# Newer macOS no longer publishes "Temperature" in the AppleSmartBattery ioreg entry,
# but the battery gas gauge still reports through IOHIDEventSystem (no root needed).
# The same service list also carries SoC die ("PMU tdie*") and NAND sensors.
_iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
_cfl = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

for _name, _res, _args in [
    ("IOHIDEventSystemClientCreate", ctypes.c_void_p, [ctypes.c_void_p]),
    ("IOHIDEventSystemClientSetMatching", None, [ctypes.c_void_p, ctypes.c_void_p]),
    ("IOHIDEventSystemClientCopyServices", ctypes.c_void_p, [ctypes.c_void_p]),
    ("IOHIDServiceClientCopyProperty", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
    ("IOHIDServiceClientCopyEvent", ctypes.c_void_p,
     [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]),
    ("IOHIDEventGetFloatValue", ctypes.c_double, [ctypes.c_void_p, ctypes.c_int32]),
]:
    getattr(_iokit, _name).restype = _res
    getattr(_iokit, _name).argtypes = _args

for _name, _res, _args in [
    ("CFStringCreateWithCString", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]),
    ("CFStringGetCString", ctypes.c_bool, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
    ("CFNumberCreate", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]),
    ("CFDictionaryCreate", ctypes.c_void_p,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]),
    ("CFArrayGetCount", ctypes.c_long, [ctypes.c_void_p]),
    ("CFArrayGetValueAtIndex", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_long]),
    ("CFRelease", None, [ctypes.c_void_p]),
]:
    getattr(_cfl, _name).restype = _res
    getattr(_cfl, _name).argtypes = _args

_CF_UTF8 = 0x08000100
_CF_NUMBER_SINT32 = 3
_HID_USAGE_PAGE_APPLE_VENDOR = 0xFF00
_HID_USAGE_TEMPERATURE_SENSOR = 5
_HID_EVENT_TYPE_TEMPERATURE = 15
# Sensor groups, matched by the service's "Product" name
_TEMP_GROUPS = {
    "battery": lambda name: name == "gas gauge battery",  # one per cell
    "soc": lambda name: "tdie" in name,                    # PMU / PMU2 die sensors
    "nand": lambda name: name.startswith("NAND"),
}


def _cfstr(text):
    return _cfl.CFStringCreateWithCString(None, text.encode(), _CF_UTF8)


def _cfint(value):
    v = ctypes.c_int32(value)
    return _cfl.CFNumberCreate(None, _CF_NUMBER_SINT32, ctypes.byref(v))


def _find_temp_services():
    keys = (ctypes.c_void_p * 2)(_cfstr("PrimaryUsagePage"), _cfstr("PrimaryUsage"))
    values = (ctypes.c_void_p * 2)(_cfint(_HID_USAGE_PAGE_APPLE_VENDOR), _cfint(_HID_USAGE_TEMPERATURE_SENSOR))
    matching = _cfl.CFDictionaryCreate(
        None, keys, values, 2,
        ctypes.addressof(ctypes.c_void_p.in_dll(_cfl, "kCFTypeDictionaryKeyCallBacks")),
        ctypes.addressof(ctypes.c_void_p.in_dll(_cfl, "kCFTypeDictionaryValueCallBacks")),
    )
    client = _iokit.IOHIDEventSystemClientCreate(None)
    _iokit.IOHIDEventSystemClientSetMatching(client, matching)
    services = _iokit.IOHIDEventSystemClientCopyServices(client)
    found = {group: [] for group in _TEMP_GROUPS}
    if not services:
        return client, found

    product_key = _cfstr("Product")
    buf = ctypes.create_string_buffer(128)
    for i in range(_cfl.CFArrayGetCount(services)):
        svc = _cfl.CFArrayGetValueAtIndex(services, i)
        name = _iokit.IOHIDServiceClientCopyProperty(svc, product_key)
        if not name:
            continue
        if _cfl.CFStringGetCString(name, buf, len(buf), _CF_UTF8):
            for group, matches in _TEMP_GROUPS.items():
                if matches(buf.value.decode()):
                    found[group].append(svc)
        _cfl.CFRelease(name)
    # client and services stay alive for the whole process so the service refs remain valid
    return client, found


_hid_client, _temp_services = _find_temp_services()


def _read_temps(group):
    temps = []
    for svc in _temp_services[group]:
        event = _iokit.IOHIDServiceClientCopyEvent(svc, _HID_EVENT_TYPE_TEMPERATURE, 0, 0)
        if not event:
            continue
        t = _iokit.IOHIDEventGetFloatValue(event, _HID_EVENT_TYPE_TEMPERATURE << 16)
        _cfl.CFRelease(event)
        if 0.0 < t < 150.0:  # drop idle / uncalibrated sensors
            temps.append(t)
    return temps


def read_battery_temperature():
    """Max over the battery gas gauge sensors (one per cell), in °C. None if unavailable."""
    temps = _read_temps("battery")
    return max(temps) if temps else None


def read_chip_temperatures():
    """SoC die max/avg and NAND max, in °C. Missing groups are None."""
    soc = _read_temps("soc")
    nand = _read_temps("nand")
    return {
        "soc_max": max(soc) if soc else None,
        "soc_avg": sum(soc) / len(soc) if soc else None,
        "nand": max(nand) if nand else None,
    }


# --- KeyboardBrightnessClient ---
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

KEYBOARD_BACKLIGHT_ID = 2


def read_keyboard_brightness():
    return _send_float_int(_kbd_brightness_client, _sel_getKeyboardBrightness, KEYBOARD_BACKLIGHT_ID)


def set_keyboard_brightness(value):
    _send_void_float_int(_kbd_brightness_client, _sel_setKeyboardBrightness, value, KEYBOARD_BACKLIGHT_ID)


def play_random_sound():
    subprocess.Popen(
        ["afplay", random.choice(SOUNDS)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def read_battery():
    out = subprocess.run(
        ["ioreg", "-r", "-c", "AppleSmartBattery", "-a"],
        capture_output=True, check=True,
    ).stdout
    items = plistlib.loads(out)
    if not items:
        return None
    data = items[0]
    nested = data.get("BatteryData", {})
    design_capacity = nested.get("DesignCapacity", data.get("DesignCapacity"))
    max_capacity = nested.get("FullChargeCapacity", data.get("AppleRawMaxCapacity"))
    temperature = data.get("Temperature")
    temperature = temperature / 100.0 if temperature is not None else read_battery_temperature()

    return {
        "temperature": temperature,
        "voltage": data["Voltage"] / 1000.0,
        "amperage": data["Amperage"],
        "percent": data["CurrentCapacity"],
        "cycle_count": data["CycleCount"],
        "is_charging": data["IsCharging"],
        "max_capacity_pct": (
            max_capacity / design_capacity * 100.0 if design_capacity else None
        ),
    }


def read_wifi():
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


class SlowMonitor:
    """Reads battery / Wi-Fi / thermal off the main thread (wdutil and ioreg can block)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"battery": None, "wifi": None, "thermal": None, "chip_temps": None}
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        readers = {
            "battery": read_battery, "wifi": read_wifi,
            "thermal": read_thermal_state, "chip_temps": read_chip_temperatures,
        }
        while not self._stop.is_set():
            for key, reader in readers.items():
                try:
                    value = reader()
                except Exception as e:
                    print(f"[{key}] read failed: {e!r}")
                    continue
                with self._lock:
                    self._data[key] = value
            self._stop.wait(SLOW_POLL_MS / 1000)

    def read(self):
        with self._lock:
            return dict(self._data)

    def stop(self):
        self._stop.set()


class PowerMonitor:
    _POWER_RE = re.compile(r"^(?:[A-Z]-)?(CPU|GPU|ANE) Power:\s*(\d+)\s*mW")
    _SAMPLE_RE = re.compile(r"^\*{3} Sampled")

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"cpu_W": 0.0, "gpu_W": 0.0, "ane_W": 0.0, "ready": False}
        self._proc = subprocess.Popen(
            ["powermetrics", "-i", "1000", "-s", "cpu_power"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        cpu_acc = 0.0
        seen_power = False
        for line in self._proc.stdout:
            line = line.strip()
            if self._SAMPLE_RE.match(line):
                # The header opens a sample, so it closes the previous one
                if seen_power:
                    with self._lock:
                        self._data["cpu_W"] = cpu_acc
                        self._data["ready"] = True
                cpu_acc = 0.0
                continue
            m = self._POWER_RE.match(line)
            if not m:
                continue
            kind, mw = m.group(1), int(m.group(2)) / 1000.0
            seen_power = True
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


def _pressure_color(v):
    # 0.0 → 파란색(#4a9eff), 1.0 → 빨간색(#ff3030)
    r = int(0x4a + (0xff - 0x4a) * v)
    g = int(0x9e + (0x30 - 0x9e) * v)
    b = int(0xff + (0x30 - 0xff) * v)
    return f"#{r:02x}{g:02x}{b:02x}"
_BAR_W = 260
# Trackpad map, matching the sensor surface (121.94 x 74.08 mm on MacBook Air M4 13")
_PAD_W = 260
_PAD_H = 158
# Canvas pixels per MTContact axis unit — tune if the ellipses look too big/small
_AXIS_PX = 2.2
_TOUCHING_STATES = (4, 5, 6)


def _ellipse_points(cx, cy, major, minor, angle, steps=24):
    # Rotated ellipse as a polygon. Canvas y points down, so the angle is negated.
    a, b = major / 2 * _AXIS_PX, minor / 2 * _AXIS_PX
    cos_t, sin_t = math.cos(-angle), math.sin(-angle)
    pts = []
    for k in range(steps):
        t = 2 * math.pi * k / steps
        ex, ey = a * math.cos(t), b * math.sin(t)
        pts += [cx + ex * cos_t - ey * sin_t, cy + ex * sin_t + ey * cos_t]
    return pts


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
        self.slow = SlowMonitor()

        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_pressure_monitor()
        self._poll()
        self._poll_slow()

    def _setup_pressure_monitor(self):
        self._mt = None
        self._mt_devices = []
        try:
            _mt = ctypes.cdll.LoadLibrary(
                "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"
            )
        except OSError as e:
            print(f"[pressure] MultitouchSupport load failed: {e}")
            return

        _cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        _cf.CFArrayGetCount.restype = ctypes.c_long
        _cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        _cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        _cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        _cf.CFRunLoopRun.restype = None
        _cf.CFRunLoopRun.argtypes = []

        _mt.MTDeviceCreateList.restype = ctypes.c_void_p
        _mt.MTRegisterContactFrameCallback.restype = None
        _mt.MTRegisterContactFrameCallback.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _mt.MTDeviceStart.restype = None
        _mt.MTDeviceStart.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _mt.MTDeviceStop.restype = None
        _mt.MTDeviceStop.argtypes = [ctypes.c_void_p]
        _mt.MTUnregisterContactFrameCallback.restype = None
        _mt.MTUnregisterContactFrameCallback.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._mt = _mt

        _MT_CB = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,  # device
            ctypes.c_void_p,  # contacts[]
            ctypes.c_int,     # nFingers
            ctypes.c_double,  # timestamp
            ctypes.c_int,     # frame
        )

        # MTContact layout (community reverse-engineering; 32/36/48/56/92 confirmed empirically)
        CONTACT_SIZE = 96
        ID_OFF = 16        # int: finger identifier, stable while the finger stays down
        STATE_OFF = 20     # int: touch state (4 = make, 5 = touching, 6 = break)
        X_OFF = 32         # normalized x (0 = left, 1 = right)
        Y_OFF = 36         # normalized y (0 = bottom, 1 = top)
        SIZE_OFF = 48      # contact size
        ANGLE_OFF = 56     # ellipse angle (radians)
        MAJOR_OFF = 60     # ellipse major axis
        MINOR_OFF = 64     # ellipse minor axis
        PRESSURE_OFF = 92

        def _read_float(addr):
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_float))[0]

        def _read_int(addr):
            return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int))[0]

        def _mt_callback(device, contacts, n_fingers, timestamp, frame):
            if contacts is None or n_fingers == 0:
                _pressure["value"] = 0.0
                _pressure["contacts"] = []
                return 0
            max_raw = 0.0
            touches = []
            for i in range(n_fingers):
                base = contacts + i * CONTACT_SIZE
                raw = _read_float(base + PRESSURE_OFF)
                touches.append({
                    "id": _read_int(base + ID_OFF),
                    "state": _read_int(base + STATE_OFF),
                    "x": _read_float(base + X_OFF),
                    "y": _read_float(base + Y_OFF),
                    "size": _read_float(base + SIZE_OFF),
                    "angle": _read_float(base + ANGLE_OFF),
                    "major": _read_float(base + MAJOR_OFF),
                    "minor": _read_float(base + MINOR_OFF),
                    "raw": raw,
                })
                if raw > max_raw:
                    max_raw = raw
            _pressure["value"] = min(max_raw / 2.0, 1.0)
            _pressure["contacts"] = touches
            return 0

        cb = _MT_CB(_mt_callback)
        self._mt_callback_ref = cb  # prevent GC

        devices = _mt.MTDeviceCreateList()
        n_devices = _cf.CFArrayGetCount(devices)

        def _run():
            for i in range(n_devices):
                dev = _cf.CFArrayGetValueAtIndex(devices, i)
                _mt.MTRegisterContactFrameCallback(dev, cb)
                _mt.MTDeviceStart(dev, 0)
                self._mt_devices.append(dev)
            print(f"[pressure] MultitouchSupport: {n_devices} device(s) started")
            _cf.CFRunLoopRun()

        threading.Thread(target=_run, daemon=True).start()

    def _stop_multitouch(self):
        # Stop devices before exit so the callback isn't invoked during interpreter
        # teardown (otherwise closing the window segfaults)
        for dev in self._mt_devices:
            self._mt.MTUnregisterContactFrameCallback(dev, self._mt_callback_ref)
            self._mt.MTDeviceStop(dev)
        self._mt_devices = []

    def _on_close(self):
        self._stop_multitouch()
        if self.kbd_brightness_saved is not None:
            set_keyboard_brightness(self.kbd_brightness_saved)
            self.kbd_brightness_saved = None
        self.power.stop()
        self.slow.stop()
        self.destroy()

    def _build_widgets(self):
        pad = {"padx": 16, "pady": 6}

        # --- Tap detection ---
        tk.Label(self, text="Acceleration delta (g)", font=("Helvetica", 12)).pack(**pad)
        self.delta_label = tk.Label(self, text="0.000", font=("Helvetica", 28))
        self.delta_label.pack()

        tk.Label(self, text="Threshold - a change larger than this is detected as a 'tap'").pack()
        tk.Scale(self, from_=0.05, to=1.0, resolution=0.01,
                 orient="horizontal", length=_BAR_W,
                 variable=self.threshold).pack(**pad)

        self.tap_label = tk.Label(self, text="Tap! 0", font=("Helvetica", 16), fg="gray")
        self.tap_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        # --- Trackpad pressure ---
        tk.Label(self, text="Trackpad Contact", font=("Helvetica", 12)).pack(**pad)
        self.pressure_canvas = tk.Canvas(
            self, width=_BAR_W, height=26,
            highlightthickness=1, highlightbackground="#aaa",
        )
        self.pressure_canvas.pack(**pad)
        self.pressure_bar = self.pressure_canvas.create_rectangle(0, 0, 0, 26, fill="#4a9eff", outline="")
        self.pressure_info = tk.Label(self, text="0.000  |  —", font=("Helvetica", 14))
        self.pressure_info.pack(**pad)
        self.pad_canvas = tk.Canvas(
            self, width=_PAD_W, height=_PAD_H, bg="#f4f4f4",
            highlightthickness=1, highlightbackground="#aaa",
        )
        self.pad_canvas.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        # --- Lid / ALS ---
        self.lid_label = tk.Label(self, text="Lid angle: measuring...", font=("Helvetica", 14))
        self.lid_label.pack(**pad)

        self.als_label = tk.Label(self, text="Light: measuring...", font=("Helvetica", 14))
        self.als_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        # --- Battery / Wi-Fi ---
        self.battery_label = tk.Label(self, text="Battery: measuring...", font=("Helvetica", 14))
        self.battery_label.pack(**pad)

        self.wifi_label = tk.Label(self, text="Wi-Fi: measuring...", font=("Helvetica", 14))
        self.wifi_label.pack(**pad)

        tk.Frame(self, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=8)

        # --- Power / Thermal ---
        self.power_label = tk.Label(self, text="Power: measuring...", font=("Helvetica", 14))
        self.power_label.pack(**pad)

        self.thermal_label = tk.Label(self, text="Thermal state: -", font=("Helvetica", 14))
        self.thermal_label.pack(**pad)

        self.chip_temp_label = tk.Label(self, text="Chip temp: measuring...", font=("Helvetica", 14))
        self.chip_temp_label.pack(**pad)

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

        # Lid angle
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

        # Ambient light
        als = self.imu.read_als()
        if als is not None:
            self.als_label.config(text=f"Light: {als.lux:.0f} lux")

        # Trackpad pressure
        pval = _pressure["value"]
        bar_w = int(_BAR_W * pval)
        self.pressure_canvas.coords(self.pressure_bar, 0, 0, bar_w, 26)
        self.pressure_canvas.itemconfig(self.pressure_bar, fill=_pressure_color(pval))
        contacts = _pressure["contacts"]
        info = f"{pval:.3f}  |  {len(contacts)} contact(s)"
        if contacts:
            c = contacts[0]
            info += f"\nmajor {c['major']:.2f}  minor {c['minor']:.2f}  angle {math.degrees(c['angle']):.0f}°  state {c['state']}"
        self.pressure_info.config(text=info)

        # Trackpad touch map
        self.pad_canvas.delete("touch")
        for c in contacts:
            cx = c["x"] * _PAD_W
            cy = (1.0 - c["y"]) * _PAD_H  # MT y grows upward, canvas y grows downward
            color = _pressure_color(min(c["raw"] / 2.0, 1.0))
            touching = c["state"] in _TOUCHING_STATES
            self.pad_canvas.create_polygon(
                _ellipse_points(cx, cy, max(c["major"], 1.0), max(c["minor"], 1.0), c["angle"]),
                # Hovering / lifting contacts are drawn as outlines only
                fill=color if touching else "", outline=color, width=1 if touching else 2,
                smooth=True, tags="touch",
            )
            self.pad_canvas.create_text(
                cx, cy, text=str(c["id"]), font=("Helvetica", 9), fill="white", tags="touch",
            )

        # CPU/GPU/ANE power
        p = self.power.read()
        if not p["ready"]:
            self.power_label.config(text="Power: measuring...")
        else:
            self.power_label.config(
                text=f"Power: CPU {p['cpu_W']:.2f}W  /  GPU {p['gpu_W']:.2f}W  /  ANE {p['ane_W']:.2f}W"
            )

        self.after(POLL_MS, self._poll)

    def _poll_slow(self):
        data = self.slow.read()

        # Battery
        battery = data["battery"]
        if battery is not None:
            state = "Charging" if battery["is_charging"] else "Discharging"
            temp_str = f"{battery['temperature']:.1f}°C" if battery["temperature"] is not None else "N/A"
            health_str = f"{battery['max_capacity_pct']:.1f}%" if battery["max_capacity_pct"] is not None else "N/A"
            self.battery_label.config(
                text=(
                    f"Battery: {battery['percent']}%  |  {temp_str}  |  "
                    f"{battery['amperage']:+d}mA ({state})  |  Cycle {battery['cycle_count']}  |  "
                    f"Max capacity {health_str}"
                )
            )

        # Wi-Fi
        wifi = data["wifi"]
        if wifi:
            self.wifi_label.config(
                text=(
                    f"Wi-Fi: {wifi.get('SSID', '-')}  |  RSSI {wifi.get('RSSI', '-')}  |  "
                    f"Channel {wifi.get('Channel', '-')}"
                )
            )

        # Thermal state
        if data["thermal"] is not None:
            self.thermal_label.config(text=f"Thermal state: {data['thermal']}")

        # SoC die / NAND temperature
        chip = data["chip_temps"]
        if chip is not None:
            fmt = lambda t: f"{t:.1f}°C" if t is not None else "N/A"
            self.chip_temp_label.config(
                text=f"Chip temp: SoC {fmt(chip['soc_max'])} (avg {fmt(chip['soc_avg'])})  |  NAND {fmt(chip['nand'])}"
            )

        self.after(SLOW_POLL_MS, self._poll_slow)


def main():
    with IMU(accel=True, gyro=False, als=True, lid=True, sample_rate=100) as imu:
        SensorUI(imu).mainloop()


if __name__ == "__main__":
    main()
