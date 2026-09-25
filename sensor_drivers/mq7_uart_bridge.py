#!/usr/bin/env python3
"""
IMM-OS MQ-7 UART bridge — CO ppm from the STM32 controller (firmware/stm32-mq7).

The STM32 runs the MQ-7 heater cycle (60 s at 5 V, 90 s at 1.4 V) and sends one
line per 150 s cycle:
    CO:12.4                                   → published as {"sensor": "mq7", "co_ppm": 12.4}
    # vout=1.234 rs=30512 r0=1105 ratio=27.6  → diagnostics, shown on stderr as {"info": ...}

  --calibrate   in clean (outdoor) air: ask the STM32 to store R0, wait for the cycle
                to finish (up to ~3 min) and print the result
Port: MQ7_PORT, default the header UART (/dev/ttyAMA0 on a Pi 5, /dev/serial0 otherwise).
Modes: stdout | mqtt | both
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from hw import uart_port  # noqa: E402
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/mq7/zone1"
BAUD_RATE = 115200
CYCLE_S = 150
_CO = re.compile(r"^CO:\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_line(line: str):
    """('co', ppm) | ('info', text) | ('bad', text) | None for a blank line."""
    line = line.strip()
    if not line:
        return None
    m = _CO.match(line)
    if m:
        return "co", float(m.group(1))
    if line.startswith("#"):
        return "info", line.lstrip("# ")
    return "bad", line


def open_port():
    import serial
    return serial.Serial(uart_port("MQ7_PORT"), BAUD_RATE, timeout=2.0)


def read_loop(ser, publish_fn):
    ser.reset_input_buffer()
    while True:
        try:
            parsed = parse_line(ser.readline().decode("utf-8", "replace"))
        except Exception as e:
            print(json.dumps({"error": f"UART read: {e}"}), file=sys.stderr, flush=True)
            time.sleep(1.0)
            continue
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "co":
            publish_fn({"sensor": "mq7", "co_ppm": round(value, 2), "timestamp": int(time.time())})
        elif kind == "info":
            print(json.dumps({"info": f"stm32: {value}"}), file=sys.stderr, flush=True)
        else:
            print(json.dumps({"error": f"Invalid STM32 data: {value!r}"}), file=sys.stderr, flush=True)


def calibrate(ser) -> int:
    ser.reset_input_buffer()
    ser.write(b"CAL\n")
    print("Calibration requested: keep the MQ-7 in clean air; waiting for the heater cycle to finish…")
    deadline = time.monotonic() + CYCLE_S + 30
    while time.monotonic() < deadline:
        parsed = parse_line(ser.readline().decode("utf-8", "replace"))
        if parsed and parsed[0] == "info":
            print("  stm32:", parsed[1])
            if parsed[1].startswith("R0="):
                print("Stored in the STM32's flash. Readings from the next cycle use it.")
                return 0
    print("No R0 reply within one cycle: check the STM32 wiring (PA9 → pin 10, PA10 → pin 8, GND).")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()
    try:
        ser = open_port()
    except Exception as e:
        print(json.dumps({"error": f"UART init: {e}"}), file=sys.stderr)
        sys.exit(1)
    if args.calibrate:
        sys.exit(calibrate(ser))
    read_loop(ser, make_publisher(args.mode, MQTT_TOPIC))


if __name__ == "__main__":
    main()
