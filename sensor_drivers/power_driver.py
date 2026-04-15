#!/usr/bin/env python3
"""
IMM-OS Power Monitor Driver (INA219)
Reads voltage and current on the main power bus at 1Hz.
Outputs JSON to stdout.
"""

import time
import json
import sys
from ina219 import INA219
from ina219 import DeviceRangeError

# Use max 16V, typical for our setup (e.g. 12V bus or 5V RPi)
SHUNT_OHMS = 0.1

def main():
    try:
        ina = INA219(SHUNT_OHMS)
        ina.configure()
    except Exception as e:
        print(json.dumps({"error": f"Failed to initialize INA219: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            voltage = ina.voltage()          # V
            current = ina.current()          # mA
            power = ina.power()              # mW
            
            payload = {
                "sensor": "ina219",
                "voltage_v": round(voltage, 3),
                "current_ma": round(current, 1),
                "power_mw": round(power, 1),
                "timestamp": int(time.time()),
            }
            print(json.dumps(payload), flush=True)

        except DeviceRangeError as e:
            print(json.dumps({"error": f"INA219 range error: {e}"}), file=sys.stderr)
        except Exception as e:
            print(json.dumps({"error": f"INA219 read error: {str(e)}"}), file=sys.stderr)

        time.sleep(1.0) # 1Hz read rate

if __name__ == '__main__':
    main()
