#!/usr/bin/env python3
"""
IMM-OS ECG driver — AD8232 heart monitor via ADS1115 channel A0. Modes: stdout | mqtt | both

Samples at ECG_SAMPLE_HZ (default 100 Hz) on a fixed schedule: the AD8232 board passes
roughly 0.5–40 Hz, so 100 Hz captures the full waveform and keeps the telemetry pipeline
comfortable. Each sample carries a millisecond timestamp so plots show the real shape.

  ECG_ADS_ADDRESS=0x48   the ECG's own ADS1115 (ADDR → GND)
  ECG_SAMPLE_HZ=100      up to 250
  ECG_LO_GPIOS=          optional "LO+,LO-" GPIO numbers: while a lead is off, nothing is
                         published (the output would just be rail noise)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/ecg_ad8232/zone1"


def open_adc():
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.ads1x15 import Mode
    from adafruit_ads1x15.analog_in import AnalogIn
    from hw import i2c_bus
    ads = ADS.ADS1115(i2c_bus(), address=int(os.getenv("ECG_ADS_ADDRESS", "0x48"), 16))
    ads.data_rate = 860
    ads.mode = Mode.CONTINUOUS          # one channel: read the latest conversion, no per-sample setup
    return AnalogIn(ads, ADS.P0)


def open_lead_off():
    spec = os.getenv("ECG_LO_GPIOS", "").strip()
    if not spec:
        return None
    from hw import digital_input
    pins = [digital_input(int(p), pull_up=False) for p in spec.split(",")]
    return lambda: any(p.value for p in pins)


def schedule(start: float, period: float, now: float, n: int):
    """Next sample index and its due time; skips ahead (no burst) if we fell behind."""
    due = start + n * period
    if now - due > 5 * period:
        n = int((now - start) / period) + 1
        due = start + n * period
    return n, due


def read_loop(publish_fn, rate_hz: float):
    try:
        chan = open_adc()
        leads_off = open_lead_off()
    except Exception as e:
        print(json.dumps({"error": f"ECG/ADC init: {e}"}), file=sys.stderr)
        sys.exit(1)

    period = 1.0 / rate_hz
    start, n, off_reported = time.monotonic(), 0, False
    while True:
        n, due = schedule(start, period, time.monotonic(), n)
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        n += 1
        try:
            if leads_off and leads_off():
                if not off_reported:
                    print(json.dumps({"error": "ECG lead off: check the electrodes"}), file=sys.stderr, flush=True)
                    off_reported = True
                continue
            off_reported = False
            publish_fn({"sensor": "ecg_ad8232", "voltage": round(chan.voltage, 4),
                        "timestamp": round(time.time(), 3)})
        except Exception as e:
            print(json.dumps({"error": f"ECG read: {e}"}), file=sys.stderr, flush=True)
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    rate = min(250.0, max(10.0, float(os.getenv("ECG_SAMPLE_HZ", "100"))))
    read_loop(make_publisher(args.mode, MQTT_TOPIC), rate)


if __name__ == "__main__":
    main()
