#!/usr/bin/env python3
"""IMM-OS Lux Driver — 3x TSL2561 via TCA9548A multiplexer. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402
ZONES = {"Zone_A": 0, "Zone_B": 1, "Zone_C": 2}

def read_loop(publish_fn):
    try:
        import board, busio
        import adafruit_tsl2561, adafruit_tca9548a
        i2c = busio.I2C(board.SCL, board.SDA)
        tca = adafruit_tca9548a.TCA9548A(i2c)
        sensors = {zone: adafruit_tsl2561.TSL2561(tca[ch]) for zone, ch in ZONES.items()}
    except Exception as e:
        print(json.dumps({"error": f"TSL2561 init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        for zone, sensor in sensors.items():
            try:
                lux = sensor.lux
                if lux is not None:
                    payload = {"sensor": "tsl2561", "zone": zone,
                               "lux": round(lux, 1), "timestamp": int(time.time())}
                    topic = f"habitat/sensors/tsl2561/{zone.lower()}"
                    publish_fn(payload, topic)
            except Exception as e:
                print(json.dumps({"error": f"TSL2561 {zone}: {e}"}), file=sys.stderr)
        time.sleep(1.0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    publish_fn = make_publisher(args.mode, None)
    read_loop(publish_fn)

if __name__ == "__main__":
    main()
