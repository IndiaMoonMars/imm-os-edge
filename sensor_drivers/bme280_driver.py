#!/usr/bin/env python3
"""
IMM-OS BME280 Sensor Driver (Temperature, Humidity, Pressure)
Outputs JSON readings to stdout every 1 second.
"""

import time
import json
import smbus2
import bme280
import sys

# Default I2C port for Raspberry Pi is 1
I2C_PORT = 1
# BME280 default I2C address
BME280_ADDRESS = 0x76

def main():
    try:
        bus = smbus2.SMBus(I2C_PORT)
        # Load calibration parameters from the sensor
        calibration_params = bme280.load_calibration_params(bus, BME280_ADDRESS)
    except Exception as e:
        # Fails gracefully if hardware is not attached
        print(json.dumps({"error": f"Failed to initialize BME280: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            # Read sensor data
            data = bme280.sample(bus, BME280_ADDRESS, calibration_params)

            payload = {
                "sensor": "bme280",
                "temp": round(data.temperature, 2),
                "hum": round(data.humidity, 2),
                "pres": round(data.pressure, 2),
                "timestamp": int(time.time()),
            }

            # JSON payload output to stdout (pipe ready)
            print(json.dumps(payload), flush=True)

        except Exception as e:
            print(json.dumps({"error": f"BME280 read error: {str(e)}"}), file=sys.stderr)

        time.sleep(1.0)

if __name__ == '__main__':
    main()
