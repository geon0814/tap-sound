"""
Measures the MTContact offset-92 field against contact size, one line per touch session.
Used to collect the "Experimental results" table in the README.
Run: venv/bin/python3 tools/measure_contact.py   (no root required)

A session runs from first contact until every finger lifts. For each session it prints
the peak raw value (offset 92) and the size (offset 48) of the contact that produced it.
Only the single highest-raw contact per frame is considered — simultaneous contacts are
not summed (see "Known bug: multi-finger touches are not aggregated" in the README).
"""

import ctypes
import threading
import time

CONTACT_SIZE = 96
SIZE_OFF = 48
PRESSURE_OFF = 92

_mt = ctypes.cdll.LoadLibrary(
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"
)
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

_MT_CB = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,  # device
    ctypes.c_void_p,  # contacts[]
    ctypes.c_int,     # nFingers
    ctypes.c_double,  # timestamp
    ctypes.c_int,     # frame
)

# (peak raw, size of the contact at that peak) for the current session
_session = {"peak": None}


def _read_float(addr):
    return ctypes.cast(addr, ctypes.POINTER(ctypes.c_float))[0]


def _mt_callback(device, contacts, n_fingers, timestamp, frame):
    if contacts is None or n_fingers == 0:
        peak = _session["peak"]
        if peak:
            raw, size = peak
            ratio = raw / size if size else float("nan")
            print(f"raw={raw:.4f}  size={size:.4f}  ratio={ratio:.4f}", flush=True)
            _session["peak"] = None
        return 0

    max_raw, at_size = 0.0, 0.0
    for i in range(n_fingers):
        base = contacts + i * CONTACT_SIZE
        raw = _read_float(base + PRESSURE_OFF)
        if raw > max_raw:
            max_raw, at_size = raw, _read_float(base + SIZE_OFF)

    peak = _session["peak"]
    if peak is None or max_raw > peak[0]:
        _session["peak"] = (max_raw, at_size)
    return 0


def main():
    cb = _MT_CB(_mt_callback)
    devices = _mt.MTDeviceCreateList()
    started = []
    for i in range(_cf.CFArrayGetCount(devices)):
        dev = _cf.CFArrayGetValueAtIndex(devices, i)
        _mt.MTRegisterContactFrameCallback(dev, cb)
        _mt.MTDeviceStart(dev, 0)
        started.append(dev)

    print(f"{len(started)} device(s) started. Touch, then lift to print a session. (Ctrl+C to quit)\n")
    try:
        # Same setup as sensor_ui.py: a background CFRunLoop drives the callbacks
        threading.Thread(target=_cf.CFRunLoopRun, daemon=True).start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Stop devices before exit so the callback isn't invoked during interpreter teardown
        for dev in started:
            _mt.MTDeviceStop(dev)
        print("\nExiting")


if __name__ == "__main__":
    main()
