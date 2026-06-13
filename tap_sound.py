"""
Plays a sound when you tap the MacBook, detected via the accelerometer
Run: sudo venv/bin/python3 tap_sound.py [threshold]
"""

import math
import random
import subprocess
import sys
import time

from macimu import IMU

# An acceleration delta (g) above this value is treated as a "tap"
THRESHOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2
# Minimum wait (seconds) before the next trigger - avoids multiple plays per tap
COOLDOWN = 0.25

SOUNDS = [
    "/System/Library/Sounds/Pop.aiff",
    "/System/Library/Sounds/Tink.aiff",
    "/System/Library/Sounds/Bottle.aiff",
    "/System/Library/Sounds/Frog.aiff",
    "/System/Library/Sounds/Funk.aiff",
    "/System/Library/Sounds/Sosumi.aiff",
]


def play_random_sound():
    subprocess.Popen(
        ["afplay", random.choice(SOUNDS)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main():
    print(f"threshold = {THRESHOLD}g  (to adjust: python3 tap_sound.py 0.3)")
    with IMU(accel=True, gyro=False, sample_rate=100) as imu:
        prev_mag = None
        last_trigger = 0.0

        print("Ready! Tap your MacBook. (Ctrl+C to quit)\n")

        while True:
            for s in imu.read_accel():
                # Magnitude of the 3-axis acceleration vector. About 1.0g at rest due to gravity
                mag = math.sqrt(s.x ** 2 + s.y ** 2 + s.z ** 2)

                if prev_mag is not None:
                    delta = abs(mag - prev_mag)
                    now = time.monotonic()

                    # Print every delta above 0.05g -- useful for picking a threshold value
                    if delta > 0.05:
                        print(f"delta={delta:.3f}g")

                    if delta > THRESHOLD and (now - last_trigger) > COOLDOWN:
                        print(f"  -> Tap detected! playing sound (delta={delta:.3f}g)")
                        play_random_sound()
                        last_trigger = now

                prev_mag = mag

            time.sleep(0.005)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting")
