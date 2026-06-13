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

    _LINE_RE = re.compile(r"^(CPU|GPU|ANE) Power:\s*(\d+)\s*mW")

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"cpu_W": 0.0, "gpu_W": 0.0, "ane_W": 0.0}
        self._proc = subprocess.Popen(
            ["powermetrics", "-i", "1000", "-s", "cpu_power"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        for line in self._proc.stdout:
            m = self._LINE_RE.match(line.strip())
            if m:
                key = {"CPU": "cpu_W", "GPU": "gpu_W", "ANE": "ane_W"}[m.group(1)]
                with self._lock:
                    self._data[key] = int(m.group(2)) / 1000.0

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
                warn = "Danger! Stop opening" if angle >= LID_DANGER_MAX else "Caution: near limit"
                self.lid_label.config(text=f"Lid angle: {angle:.1f}°  -  {warn}", fg="red")
                self.lid_beep_cooldown -= POLL_MS
                if self.lid_beep_cooldown <= 0:
                    subprocess.Popen(
                        ["afplay", LID_BEEP_SOUND],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    self.lid_beep_cooldown = interval
            else:
                self.lid_label.config(text=f"Lid angle: {angle:.1f}°", fg="black")
                self.lid_beep_cooldown = 0

        # Ambient light (a new value only arrives when it changes)
        als = self.imu.read_als()
        if als is not None:
            self.als_label.config(text=f"Light: {als.lux:.0f} lux")

        # CPU/GPU/ANE power (continuously updated by the background powermetrics thread)
        p = self.power.read()
        self.power_label.config(
            text=f"Power: CPU {p['cpu_W']:.2f}W  /  GPU {p['gpu_W']:.2f}W  /  ANE {p['ane_W']:.2f}W"
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
                    f"{battery['amperage']:+d}mA ({state})  |  Cycle {battery['cycle_count']}"
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
