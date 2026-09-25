#!/usr/bin/env python3
"""
UWB driver — Qorvo/Decawave DWM1001 (MDEK1001) tag over its UART shell.

The tag computes its own position from the anchors (configure anchors and their
positions with the Decawave app or shell first). This driver opens the shell
(two Enter presses), starts `lec` output (CSV with a POS block per update, ~10 Hz)
and publishes x/y/z (metres) and the quality factor to habitat/eva/uwb.

Connect the DWM1001-DEV board by USB (/dev/ttyACM0) or its UART pins (/dev/serial0).

Environment:
  UWB_PORT=/dev/ttyACM0  UWB_BAUD=115200  CREW_ID=ev1  + MQTT_*

  --simulate   circle around a 10 × 10 m habitat at 5 Hz (the old behaviour)
"""
import argparse
import json
import logging
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from eva_mqtt import connect, crew_id  # noqa: E402
from hw import env_int, simulate_requested  # noqa: E402
from positioning import parse_dwm_lec  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [uwb_driver] %(message)s")
log = logging.getLogger(__name__)

TOPIC = "habitat/eva/uwb"
GRID_W = GRID_H = 10.0


def positions_from_dwm1001():
    import serial
    port, baud = os.getenv("UWB_PORT", "/dev/ttyACM0"), env_int("UWB_BAUD", 115200)
    with serial.Serial(port, baud, timeout=2) as ser:
        ser.write(b"\r")
        time.sleep(0.1)
        ser.write(b"\r")          # two Enters within 1 s open the shell
        time.sleep(1.0)
        ser.reset_input_buffer()
        ser.write(b"lec\r")       # start CSV position output
        log.info("DWM1001 shell on %s: lec output started", port)
        silent_since = time.monotonic()
        while True:
            line = ser.readline().decode("ascii", "replace")
            pos = parse_dwm_lec(line) if line else None
            if pos:
                silent_since = time.monotonic()
                yield pos
            elif time.monotonic() - silent_since > 10:
                log.warning("No UWB position for 10 s (anchors in range? tag in shell mode?); re-sending lec")
                ser.write(b"lec\r")
                silent_since = time.monotonic()


def positions_simulated():
    step = 0
    while True:
        t = step * 0.2
        yield {"x_m": round(GRID_W / 2 + 3.5 * math.cos(t * 0.3) + random.uniform(-0.03, 0.03), 3),
               "y_m": round(GRID_H / 2 + 3.5 * math.sin(t * 0.3) + random.uniform(-0.03, 0.03), 3),
               "z_m": round(1.2 + random.uniform(-0.02, 0.02), 3),
               "quality": random.randint(85, 100)}
        step += 1
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    crew = crew_id()
    client = connect(f"uwb-{crew}")
    source = positions_simulated() if simulate_requested(args.simulate) else positions_from_dwm1001()
    log.info("UWB for %s → %s", crew, TOPIC)
    for pos in source:
        client.publish(TOPIC, json.dumps({"crew_id": crew, "source": "uwb", **pos, "timestamp": int(time.time())}))


if __name__ == "__main__":
    main()
