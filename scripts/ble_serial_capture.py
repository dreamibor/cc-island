#!/usr/bin/env python3
"""Passive ESP32 serial log capture for BLE debugging (run from any cwd).

Usage:  python ble_serial_capture.py [PORT=COM3] [SECONDS=180]
Resets the chip first (comment out the esptool block for passive-only),
captures the boot/app log to serial_capture.log in the current directory.
Requires pyserial + esptool (both ship with the ESP-IDF python env).
"""
import time

PORT = "COM3"
BAUD = 115200
DURATION = 180

import esptool
# reset the chip (runs the app fresh), releasing the port afterwards
sys.argv = ["esptool", "--port", PORT, "--before", "default-reset", "--after", "hard-reset", "chip-id"]
try:
    esptool.main()
except SystemExit:
    pass
time.sleep(1.0)

import serial
ser = serial.Serial(PORT, BAUD, timeout=1)
print(f"--- capturing {PORT} for {DURATION}s ---", flush=True)
deadline = time.time() + DURATION
buf = b""
while time.time() < deadline:
    chunk = ser.read(512)
    if chunk:
        buf += chunk
        try:
            text = chunk.decode("utf-8", errors="replace")
            print(text, end="", flush=True)
        except Exception:
            pass
ser.close()
open(r"C:\Users\ZHAOYU\AppData\Local\Temp\ccisland_selftest\serial_capture.log", "wb").write(buf)
print("\n--- capture saved ---", flush=True)