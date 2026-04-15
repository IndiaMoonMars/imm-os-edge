#!/usr/bin/env python3
"""
IMM-OS Lux Sensor Driver (Multiplexed TSL2561)
Reads from 3 TSL2561 sensors for 3 different habitat zones.
Since TSL2561 shares standard I2C addresses, we assume a TCA9548A multiplexer is used.
Outputs JSON to stdout.
"""

import time
import json
import sys
import board
import busio
import adafruit_tsl2561
import adafruit_tca9548a

def main():
    try:
        # Create I2C bus
        i2c = busio.I2C(board.SCL, board.SDA)
        
        # Create the TCA9548A multiplexer
        tca = adafruit_tca9548a.TCA9548A(i2c)
        
        # Initialize 3 TSL2561 sensors on channels 0, 1, and 2
        sensor_zone_A = adafruit_tsl2561.TSL2561(tca[0])
        sensor_zone_B = adafruit_tsl2561.TSL2561(tca[1])
        sensor_zone_C = adafruit_tsl2561.TSL2561(tca[2])
        
        sensors = {
            "Zone_A": sensor_zone_A,
            "Zone_B": sensor_zone_B,
            "Zone_C": sensor_zone_C
        }

    except Exception as e:
        print(json.dumps({"error": f"Failed to initialize TSL2561 array: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            for zone, sensor in sensors.items():
                lux = sensor.lux
                
                if lux is not None:
                    payload = {
                        "sensor": "tsl2561",
                        "zone": zone,
                        "lux": round(lux, 1),
                        "timestamp": int(time.time()),
                    }
                    print(json.dumps(payload), flush=True)

        except Exception as e:
            print(json.dumps({"error": f"TSL2561 read error: {str(e)}"}), file=sys.stderr)

        time.sleep(1.0) # 1Hz read rate

if __name__ == '__main__':
    main()
