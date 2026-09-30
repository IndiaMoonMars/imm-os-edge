#!/usr/bin/env python3
"""IMM-OS SCD40 Driver — CO₂ / Temp / Humidity. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402
MQTT_TOPIC = "habitat/sensors/scd40/zone1"

def read_loop(publish_fn):
    try:
        import adafruit_scd4x
        from hw import i2c_bus
        i2c = i2c_bus()
        scd40 = adafruit_scd4x.SCD4X(i2c)
        scd40.start_periodic_measurement()
    except Exception as e:
        print(json.dumps({"error": f"SCD40 init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        try:
            if scd40.data_ready:
                payload = {
                    "sensor": "scd40",
                    "co2_ppm": scd40.CO2,
                    "temp": round(scd40.temperature, 2),
                    "hum": round(scd40.relative_humidity, 2),
                    "timestamp": int(time.time()),
                }
                publish_fn(payload)
        except Exception as e:
            print(json.dumps({"error": f"SCD40 read: {e}"}), file=sys.stderr)
        time.sleep(5.0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    publish_fn = make_publisher(args.mode, MQTT_TOPIC)
    read_loop(publish_fn)

if __name__ == "__main__":
    main()
