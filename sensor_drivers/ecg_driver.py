#!/usr/bin/env python3
"""IMM-OS ECG Driver — AD8232 via ADS1115 at 250Hz. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402
MQTT_TOPIC = "habitat/sensors/ecg_ad8232/zone1"

def read_loop(publish_fn):
    try:
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        from hw import i2c_bus
        i2c = i2c_bus()
        ads = ADS.ADS1115(i2c, address=int(os.getenv("ECG_ADS_ADDRESS", "0x48"), 16))
        ads.data_rate = 860
        chan = AnalogIn(ads, ADS.P0)
    except Exception as e:
        print(json.dumps({"error": f"ECG/ADC init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        try:
            start = time.time()
            payload = {"sensor": "ecg_ad8232", "voltage": round(chan.voltage, 4),
                       "timestamp": time.time()}
            publish_fn(payload)
            elapsed = time.time() - start
            time.sleep(max(0, 0.004 - elapsed))
        except Exception as e:
            print(json.dumps({"error": f"ECG read: {e}"}), file=sys.stderr)
            time.sleep(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    publish_fn = make_publisher(args.mode, MQTT_TOPIC)
    read_loop(publish_fn)

if __name__ == "__main__":
    main()
