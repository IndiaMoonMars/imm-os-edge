#!/usr/bin/env python3
"""IMM-OS MQ-7 UART Bridge — CO ppm from STM32. Modes: stdout | mqtt"""

import argparse, sys, time, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
MQTT_TOPIC = "habitat/sensors/mq7/zone1"
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

def read_loop(publish_fn):
    try:
        import serial
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2.0)
        ser.flushInput()
    except Exception as e:
        print(json.dumps({"error": f"UART init: {e}"}), file=sys.stderr); sys.exit(1)

    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8').strip()
                if line:
                    co_ppm = float(line.split(':')[-1].strip())
                    payload = {"sensor": "mq7", "co_ppm": round(co_ppm, 2), "timestamp": int(time.time())}
                    publish_fn(payload)
        except ValueError:
            print(json.dumps({"error": f"Invalid STM32 data: {line}"}), file=sys.stderr)
        except Exception as e:
            print(json.dumps({"error": f"UART read: {e}"}), file=sys.stderr)
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
