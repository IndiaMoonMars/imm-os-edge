#!/usr/bin/env python3
"""
IMM-OS Biosensor Driver — MAX30100/MAX30102 heart rate + SpO2 (habitat crew station).
Modes: stdout | mqtt | both

Samples red/IR light at 100 Hz and publishes HR/SpO2 once a second, computed over the
last 5 s (core/biometrics.py). Nothing is published while no finger is on the sensor.
"""
import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from biometrics import heart_rate, spo2  # noqa: E402
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/max30100/zone1"
FS, WINDOW_S = 100, 5


def read_loop(publish_fn):
    try:
        from max30100 import MAX30100   # core/max30100.py
        mx30 = MAX30100()
        mx30.enable_spo2()
    except Exception as e:
        print(json.dumps({"error": f"MAX30100 init: {e}"}), file=sys.stderr)
        sys.exit(1)

    ir = collections.deque(maxlen=FS * WINDOW_S)
    red = collections.deque(maxlen=FS * WINDOW_S)
    next_publish = time.monotonic() + 1
    while True:
        try:
            for s_ir, s_red in mx30.read_fifo():   # exactly FS samples per second
                ir.append(s_ir)
                red.append(s_red)
        except Exception as e:
            print(json.dumps({"error": f"MAX30100 read: {e}"}), file=sys.stderr)
            time.sleep(0.5)
        now = time.monotonic()
        if now >= next_publish and len(ir) == ir.maxlen:
            next_publish = now + 1
            hr, ox = heart_rate(list(ir), FS), spo2(list(red), list(ir))
            if hr is not None:
                payload = {"sensor": "max30100", "hr_bpm": hr, "timestamp": int(time.time())}
                if ox is not None:
                    payload["spo2_pct"] = ox
                publish_fn(payload)
        time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    read_loop(make_publisher(args.mode, MQTT_TOPIC))


if __name__ == "__main__":
    main()
