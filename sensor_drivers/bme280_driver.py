#!/usr/bin/env python3
"""
IMM-OS BME280 Sensor Driver (Temperature, Humidity, Pressure)
Modes:
  --mode stdout  (default)  — prints JSON to stdout (Phase 1 pipe mode)
  --mode mqtt               — publishes to MQTT broker via env MQTT_HOST/MQTT_PORT
"""

import argparse
import sys
import time
import json
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

I2C_PORT = int(os.getenv("I2C_BUS", "1"))
BME280_ADDRESS = int(os.getenv("BME280_ADDRESS", "0x76"), 16)   # 0x77 on Adafruit-style boards (SDO high)
MQTT_TOPIC = "habitat/sensors/bme280/zone1"


def read_loop(publish_fn):
    try:
        import smbus2
        import bme280
        bus = smbus2.SMBus(I2C_PORT)
        calibration_params = bme280.load_calibration_params(bus, BME280_ADDRESS)
    except Exception as e:
        print(json.dumps({"error": f"BME280 init failed: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            data = bme280.sample(bus, BME280_ADDRESS, calibration_params)
            payload = {
                "sensor": "bme280",
                "temp": round(data.temperature, 2),
                "hum": round(data.humidity, 2),
                "pres": round(data.pressure, 2),
                "timestamp": int(time.time()),
            }
            publish_fn(payload)
        except Exception as e:
            print(json.dumps({"error": f"BME280 read: {e}"}), file=sys.stderr)
        time.sleep(1.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()

    publish_fn = make_publisher(args.mode, MQTT_TOPIC)

    read_loop(publish_fn)


if __name__ == "__main__":
    main()
