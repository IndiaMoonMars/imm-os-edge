#!/usr/bin/env python3
"""
IMM-OS O2 Driver — galvanic O2 cell (ME2-O2, KE-25, SK-25F…) via ADS1115 channel A1.
Modes: stdout | mqtt | both

A galvanic cell outputs a small voltage proportional to O2 partial pressure. Calibrate
in fresh outdoor air (20.9 % O2): run with --calibrate, note the millivolts it prints,
and set O2_CAL_MV to that value in /etc/imm-os/edge.env. Re-calibrate monthly; cells
drift and wear out (typical life 2 years).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/o2/zone1"
AIR_O2_PCT = 20.9
ADS_ADDRESS = int(os.getenv("O2_ADS_ADDRESS", "0x48"), 16)   # shared with the ECG ADS1115
SAMPLES = 16  # averaged per reading; the cell output is noisy at µV level


def open_channel():
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
    from hw import i2c_bus
    ads = ADS.ADS1115(i2c_bus(), address=ADS_ADDRESS)
    ads.gain = 16  # ±0.256 V full scale: galvanic cells output ~10–60 mV
    return AnalogIn(ads, ADS.P1)


def read_mv(chan) -> float:
    total = 0.0
    for _ in range(SAMPLES):
        total += chan.voltage
        time.sleep(0.01)
    return total / SAMPLES * 1000.0


def o2_percent(mv: float, cal_mv: float) -> float:
    """Linear cell response: cal_mv corresponds to 20.9 % O2 in air."""
    return mv / cal_mv * AIR_O2_PCT


def read_loop(publish_fn, cal_mv: float):
    try:
        chan = open_channel()
    except Exception as e:
        print(json.dumps({"error": f"O2/ADC init: {e}"}), file=sys.stderr)
        sys.exit(1)
    while True:
        try:
            mv = read_mv(chan)
            publish_fn({"sensor": "o2", "o2_pct": round(o2_percent(mv, cal_mv), 2), "timestamp": int(time.time())})
        except Exception as e:
            print(json.dumps({"error": f"O2 read: {e}"}), file=sys.stderr)
        time.sleep(2.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--calibrate", action="store_true", help="print the cell voltage in fresh air and exit")
    args = parser.parse_args()
    if args.calibrate:
        chan = open_channel()
        readings = [read_mv(chan) for _ in range(10)]
        print(f"O2 cell in air: {sum(readings) / len(readings):.3f} mV  →  set O2_CAL_MV to this value")
        return
    cal = os.getenv("O2_CAL_MV")
    if not cal:
        print(json.dumps({"error": "O2_CAL_MV not set; run with --calibrate in fresh air first"}), file=sys.stderr)
        sys.exit(1)
    read_loop(make_publisher(args.mode, MQTT_TOPIC), float(cal))


if __name__ == "__main__":
    main()
