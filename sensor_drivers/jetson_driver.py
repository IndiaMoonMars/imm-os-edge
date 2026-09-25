#!/usr/bin/env python3
"""
IMM-OS Jetson Driver — on-board CPU/GPU temperature and total power (Jetson Orin / Xavier).
Optional: the compute node is a Raspberry Pi 5 (sysmon_driver.py); keep this for a Jetson added later.
Modes: stdout | mqtt | both

Reads Linux sysfs only (no extra packages):
  temperature  /sys/class/thermal/thermal_zone*/{type,temp}  (types like cpu-thermal, gpu-thermal)
  power        INA3221 rails under /sys/bus/i2c/drivers/ina3221/*/hwmon/hwmon*/
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/jetson/compute"
THERMAL_ROOT = os.getenv("JETSON_THERMAL_ROOT", "/sys/class/thermal")
INA_GLOB = os.getenv("JETSON_INA_GLOB", "/sys/bus/i2c/drivers/ina3221/*/hwmon/hwmon*")


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def zone_temp(kind: str):
    """Temperature (°C) of the first thermal zone whose type starts with kind (cpu/gpu)."""
    for zone in sorted(glob.glob(os.path.join(THERMAL_ROOT, "thermal_zone*"))):
        try:
            if _read(os.path.join(zone, "type")).lower().startswith(kind):
                return round(int(_read(os.path.join(zone, "temp"))) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None


def total_power_w():
    """Sum V×I over the INA3221 input rails (hwmon reports mV and mA)."""
    total, found = 0.0, False
    for hw in glob.glob(INA_GLOB):
        for vin in glob.glob(os.path.join(hw, "in[1-3]_input")):
            idx = vin[-7]
            cur = os.path.join(hw, f"curr{idx}_input")
            try:
                total += int(_read(vin)) * int(_read(cur)) / 1e6
                found = True
            except (OSError, ValueError):
                continue
    return round(total, 2) if found else None


def read_once() -> dict:
    payload = {"sensor": "jetson", "timestamp": int(time.time())}
    for key, value in (("cpu_temp", zone_temp("cpu")), ("gpu_temp", zone_temp("gpu")), ("power_w", total_power_w())):
        if value is not None:
            payload[key] = value
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    publish_fn = make_publisher(args.mode, MQTT_TOPIC)
    while True:
        payload = read_once()
        if len(payload) > 2:
            publish_fn(payload)
        else:
            print(json.dumps({"error": "no Jetson thermal/power readings found"}), file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
