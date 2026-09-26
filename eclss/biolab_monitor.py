#!/usr/bin/env python3
"""
Biolab monitor — Atlas Scientific EZO-pH circuit (I2C) + DS18B20 water temperature (1-Wire).

Every BIOLAB_INTERVAL_S the DS18B20 is read first, then the EZO-pH takes a reading
temperature-compensated to that water temperature ("RT,<temp>" command), and both
are sent to the ECLSS API.

Wiring / setup:
  EZO-pH in I2C mode (default address 0x63); enable I2C (raspi-config).
  DS18B20 data on GPIO4 with a 4.7 kΩ pull-up; add `dtoverlay=w1-gpio` to
  /boot/firmware/config.txt. Calibrate the pH probe with the EZO's own
  mid/low/high buffer commands (see Atlas datasheet) before use.

Environment:
  EZO_PH_ADDRESS=0x63  I2C_BUS=1  BIOLAB_INTERVAL_S=60  ECLSS_API_URL=http://imm.local/eclss

  --simulate   pH 7.0±0.1 and 24.5±0.2 °C (the old behaviour)
"""
import argparse
import glob
import logging
import os
import random
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from calibration import default as calibration  # noqa: E402
from hw import EventPoster, env_float, env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [biolab_monitor] %(message)s")
log = logging.getLogger(__name__)

ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/biolab/log"
W1_GLOB = os.getenv("DS18B20_GLOB", "/sys/bus/w1/devices/28-*/w1_slave")


def parse_ds18b20(text: str) -> Optional[float]:
    """Parse a w1_slave file: first line must end in YES (CRC ok); second has t=<milli °C>."""
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].strip().endswith("YES") or "t=" not in lines[1]:
        return None
    milli = int(lines[1].split("t=")[1])
    if milli in (85000, -127000):      # power-on reset / disconnected sentinel values
        return None
    return milli / 1000.0


def read_ds18b20() -> Optional[float]:
    for path in glob.glob(W1_GLOB):
        for _ in range(3):
            try:
                with open(path) as f:
                    t = parse_ds18b20(f.read())
            except OSError:
                t = None
            if t is not None:
                return t
            time.sleep(0.2)
    return None


def parse_ezo_response(data: bytes) -> Optional[float]:
    """EZO I2C response: status byte (1 = success) then ASCII value, NUL padded."""
    if not data or data[0] != 1:
        return None
    text = bytes(data[1:]).split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    try:
        return float(text)
    except ValueError:
        return None


def read_ezo_ph(bus_no: int, address: int, temp_c: Optional[float]) -> Optional[float]:
    from smbus2 import SMBus, i2c_msg
    cmd = f"RT,{temp_c:.1f}" if temp_c is not None else "R"
    with SMBus(bus_no) as bus:
        bus.i2c_rdwr(i2c_msg.write(address, cmd.encode()))
        time.sleep(0.9)                               # reading takes 900 ms
        msg = i2c_msg.read(address, 31)
        bus.i2c_rdwr(msg)
        return parse_ezo_response(bytes(msg))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    simulate = simulate_requested(args.simulate)
    poster = EventPoster(API_URL, "biolab")
    interval = env_float("BIOLAB_INTERVAL_S", 60)
    bus_no, address = env_int("I2C_BUS", 1), env_int("EZO_PH_ADDRESS", 0x63)
    log.info("Biolab monitor (%s)", "simulated" if simulate else f"EZO-pH 0x{address:02X}, DS18B20")

    while True:
        if simulate:
            ph, temp = 7.0 + random.uniform(-0.1, 0.1), 24.5 + random.uniform(-0.2, 0.2)
        else:
            temp = read_ds18b20()
            if temp is not None:
                temp = calibration().correct("ds18b20", "temp", temp)   # calibration.yaml
            try:
                ph = read_ezo_ph(bus_no, address, temp)
                if ph is not None:
                    ph = calibration().correct("ezo_ph", "ph", ph)
            except OSError as exc:
                log.error("EZO-pH I2C error: %s", exc)
                ph = None
            if temp is None:
                log.warning("DS18B20 not found or CRC failed; pH read uncompensated")
        if ph is None:
            log.warning("No valid pH reading this cycle")
        elif not 0 <= ph <= 14:
            log.error("pH %.2f out of range — probe fault or needs calibration", ph)
        elif temp is None:
            # the ECLSS API stores pH and water temperature together
            log.warning("pH %.2f not sent: no water temperature (check the DS18B20)", ph)
        else:
            log.info("pH %.2f | water %.2f °C", ph, temp)
            poster.post({"ph_level": round(ph, 2), "water_temp_c": round(temp, 2)})
        time.sleep(interval)


if __name__ == "__main__":
    main()
