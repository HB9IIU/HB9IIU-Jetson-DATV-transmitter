"""Minimal GPIO ON/OFF blink test for Jetson Nano, for later use as PTT.

Run directly (no arguments needed) - blinks pin 18 forever until Ctrl+C.

Requires: pip install Jetson.GPIO
Pin numbering: BOARD (physical pin numbers).
"""

import time

import Jetson.GPIO as GPIO

PIN = 18  # physical (BOARD) pin number, change to match wiring

GPIO.setmode(GPIO.BOARD)
GPIO.setup(PIN, GPIO.OUT, initial=GPIO.LOW)

print(f"Blinking pin {PIN} (BOARD numbering). Press Ctrl+C to stop.")

try:
    while True:
        GPIO.output(PIN, GPIO.HIGH)
        print("ON")
        time.sleep(0.5)
        GPIO.output(PIN, GPIO.LOW)
        print("OFF")
        time.sleep(0.5)
except KeyboardInterrupt:
    print("Stopping...")
finally:
    GPIO.cleanup()
    print("GPIO cleaned up.")
