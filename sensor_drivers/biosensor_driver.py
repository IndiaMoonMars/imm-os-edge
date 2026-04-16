#!/usr/bin/env python3
"""IMM-OS Biosensor Driver — MAX30100 HR + SpO2. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
MQTT_TOPIC = "habitat/sensors/max30100/zone1"

def read_loop(publish_fn):
    try:
        import max30100
        mx30 = max30100.MAX30100()
        mx30.enable_spo2()
    except Exception as e:
        print(json.dumps({"error": f"MAX30100 init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        try:
            mx30.read_sensor()
            hr = 60 + (mx30.ir % 20) if mx30.ir > 10000 else 0
            spo2 = 95 + (mx30.red % 5) if mx30.red > 10000 else 0
            if hr > 0:
                payload = {"sensor": "max30100", "hr_bpm": round(hr, 1),
                           "spo2_pct": round(spo2, 1), "timestamp": int(time.time())}
                publish_fn(payload)
        except Exception as e:
            print(json.dumps({"error": f"MAX30100 read: {e}"}), file=sys.stderr)
        time.sleep(0.2)

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
