#!/usr/bin/env python3
"""IMM-OS ECG Driver — AD8232 via ADS1115 at 250Hz. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
MQTT_TOPIC = "habitat/sensors/ecg/zone1"

def read_loop(publish_fn):
    try:
        import board, busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        i2c = busio.I2C(board.SCL, board.SDA)
        ads = ADS.ADS1115(i2c)
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
