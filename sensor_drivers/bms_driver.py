#!/usr/bin/env python3
"""
IMM-OS BMS driver — battery state of charge and solar input for the compute/power node.

Publishes {"sensor": "bms", "battery_pct", "solar_w"} on habitat/sensors/bms/<zone>.

  Battery: a MAX17048/MAX17040 fuel gauge at 0x36, as on the Geekworm X120x Pi 5 UPS
           boards and most LiPo "fuel gauge" breakouts (SOC register, % with 1/256 steps).
  Solar:   an INA219 on the solar panel → charge-controller line, at its own address
           (e.g. 0x44 with A1 bridged, so it doesn't clash with the bus INA219 / PCA9685).

Environment:
  BMS_GAUGE_ADDRESS=0x36        set to "none" if there is no fuel gauge
  BMS_SOLAR_INA219_ADDRESS=     empty = no solar sensor
  BMS_SOLAR_SHUNT_OHMS=0.1
  I2C_BUS=1

Modes: stdout | mqtt | both
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from hw import env_float, i2c_bus_number  # noqa: E402
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/bms/power"
REG_SOC = 0x04


def soc_from_bytes(hi: int, lo: int) -> float:
    """MAX1704x SOC register: high byte = whole %, low byte = 1/256 %; clamped to 0–100."""
    return round(min(100.0, max(0.0, hi + lo / 256.0)), 1)


def _address(name: str, default: str):
    raw = os.getenv(name, default).strip().lower()
    return None if raw in ("", "none", "off") else int(raw, 0)


class FuelGauge:
    def __init__(self, address: int, bus=None):
        if bus is None:
            from smbus2 import SMBus
            bus = SMBus(i2c_bus_number())
        self.bus, self.address = bus, address

    def battery_pct(self) -> float:
        hi, lo = self.bus.read_i2c_block_data(self.address, REG_SOC, 2)   # big-endian register
        return soc_from_bytes(hi, lo)


class SolarMeter:
    def __init__(self, address: int):
        from ina219 import INA219
        self.ina = INA219(env_float("BMS_SOLAR_SHUNT_OHMS", 0.1), busnum=i2c_bus_number(), address=address)
        self.ina.configure()

    def solar_w(self) -> float:
        return round(max(0.0, self.ina.power()) / 1000.0, 2)   # mW → W; reverse current reads as 0


def read_once(gauge, solar) -> dict:
    payload = {"sensor": "bms", "timestamp": int(time.time())}
    for key, source in (("battery_pct", gauge and gauge.battery_pct), ("solar_w", solar and solar.solar_w)):
        if source:
            try:
                payload[key] = source()
            except OSError as e:
                print(json.dumps({"error": f"BMS {key}: {e}"}), file=sys.stderr)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args()

    gauge_addr = _address("BMS_GAUGE_ADDRESS", "0x36")
    solar_addr = _address("BMS_SOLAR_INA219_ADDRESS", "")
    if gauge_addr is None and solar_addr is None:
        sys.exit("bms_driver: set BMS_GAUGE_ADDRESS and/or BMS_SOLAR_INA219_ADDRESS")
    try:
        gauge = FuelGauge(gauge_addr) if gauge_addr is not None else None
        solar = SolarMeter(solar_addr) if solar_addr is not None else None
    except Exception as e:
        print(json.dumps({"error": f"BMS init: {e}"}), file=sys.stderr)
        sys.exit(1)

    publish_fn = make_publisher(args.mode, MQTT_TOPIC)
    while True:
        payload = read_once(gauge, solar)
        if len(payload) > 2:
            publish_fn(payload)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
