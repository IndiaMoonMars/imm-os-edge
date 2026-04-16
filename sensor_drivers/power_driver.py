#!/usr/bin/env python3
"""IMM-OS Power Monitor Driver — INA219. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
MQTT_TOPIC = "habitat/sensors/ina219/power_bus"
SHUNT_OHMS = 0.1

def read_loop(publish_fn):
    try:
        from ina219 import INA219
        ina = INA219(SHUNT_OHMS)
        ina.configure()
    except Exception as e:
        print(json.dumps({"error": f"INA219 init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        try:
            from ina219 import DeviceRangeError
            payload = {"sensor": "ina219", "voltage_v": round(ina.voltage(), 3),
                       "current_ma": round(ina.current(), 1),
                       "power_mw": round(ina.power(), 1),
                       "timestamp": int(time.time())}
            publish_fn(payload)
        except Exception as e:
            print(json.dumps({"error": f"INA219 read: {e}"}), file=sys.stderr)
        time.sleep(1.0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["stdout", "mqtt"], default="stdout")
    args = parser.parse_args()
    if args.mode == "mqtt":
        from mqtt_publisher import create_client, publish as mp
        c = create_client(); publish_fn = lambda p: mp(c, MQTT_TOPIC, p)
    else:
        publish_fn = lambda p: print(json.dumps(p), flush=True)
    read_loop(publish_fn)

if __name__ == "__main__":
    main()
