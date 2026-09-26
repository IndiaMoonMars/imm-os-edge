#!/usr/bin/env python3
"""
IMM-OS lux driver — TSL2561 light sensors behind a TCA9548A I2C multiplexer (0x70).
Modes: stdout | mqtt | both

One reading per zone per second on habitat/sensors/tsl2561/<zone>.

  LUX_ZONES="zone_a:0,zone_b:1,zone_c:2"   zone:mux-channel pairs
  LUX_MUX_ADDRESS=0x70

A sensor that is missing or stops answering is reported and skipped; the others keep
publishing, and it is retried every 30 s.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

RETRY_S = 30


def parse_zones(spec: str) -> dict:
    """'zone_a:0,zone_b:1' → {'zone_a': 0, 'zone_b': 1}; zone names lower-cased."""
    zones = {}
    for part in (spec or "").split(","):
        if ":" in part:
            name, ch = part.split(":", 1)
            ch = int(ch)
            if not 0 <= ch <= 7:
                raise ValueError(f"mux channel {ch} out of range 0-7")
            zones[name.strip().lower()] = ch
    return zones


def err(msg):
    print(json.dumps({"error": msg}), file=sys.stderr, flush=True)


def read_loop(publish_fn, zones: dict):
    try:
        import adafruit_tca9548a
        import adafruit_tsl2561
        from hw import i2c_bus
        tca = adafruit_tca9548a.TCA9548A(i2c_bus(), address=int(os.getenv("LUX_MUX_ADDRESS", "0x70"), 16))
    except Exception as e:
        err(f"TCA9548A init: {e}")
        sys.exit(1)

    sensors, retry_at = {}, {}

    def attach(zone):
        try:
            sensors[zone] = adafruit_tsl2561.TSL2561(tca[zones[zone]])
            retry_at.pop(zone, None)
        except Exception as e:
            sensors.pop(zone, None)
            retry_at[zone] = time.monotonic() + RETRY_S
            err(f"TSL2561 {zone} (mux channel {zones[zone]}): {e}")

    for zone in zones:
        attach(zone)
    if not sensors:
        err("no TSL2561 found on any mux channel")
        sys.exit(1)

    while True:
        now = time.monotonic()
        for zone in [z for z, t in retry_at.items() if now >= t]:
            attach(zone)
        for zone, sensor in list(sensors.items()):
            try:
                lux = sensor.lux          # None while saturated (very bright light)
                if lux is not None:
                    publish_fn({"sensor": "tsl2561", "zone": zone, "lux": round(lux, 1),
                                "timestamp": int(time.time())}, f"habitat/sensors/tsl2561/{zone}")
            except Exception as e:
                err(f"TSL2561 {zone}: {e}")
                sensors.pop(zone, None)
                retry_at[zone] = now + RETRY_S
        time.sleep(1.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    args = parser.parse_args()
    zones = parse_zones(os.getenv("LUX_ZONES", "zone_a:0,zone_b:1,zone_c:2"))
    read_loop(make_publisher(args.mode, None), zones)


if __name__ == "__main__":
    main()
