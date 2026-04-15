#!/usr/bin/env python3
"""
IMM-OS SCD40 Sensor Driver (CO₂, Temperature, Humidity)
Reads from the SCD40 via I2C every 5 seconds.
Outputs JSON to stdout.
"""

import time
import json
import sys
import board
import adafruit_scd4x

def main():
    try:
        # Create I2C bus object
        i2c = board.I2C()  # Uses board.SCL and board.SDA
        scd40 = adafruit_scd4x.SCD4X(i2c)
        scd40.start_periodic_measurement()
        
    except Exception as e:
        print(json.dumps({"error": f"Failed to initialize SCD40: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            if scd40.data_ready:
                payload = {
                    "sensor": "scd40",
                    "co2_ppm": scd40.CO2,
                    "temp": round(scd40.temperature, 2) if scd40.temperature else None,
                    "hum": round(scd40.relative_humidity, 2) if scd40.relative_humidity else None,
                    "timestamp": int(time.time()),
                }
                
                if payload["co2_ppm"]:
                    print(json.dumps(payload), flush=True)

        except Exception as e:
            print(json.dumps({"error": f"SCD40 read error: {str(e)}"}), file=sys.stderr)

        time.sleep(5.0)

if __name__ == '__main__':
    main()
