#!/usr/bin/env python3
"""
GPS driver — u-blox NEO-M9N over UART, NMEA output.

Reads RMC (position, validity) and GGA (fix quality, satellites, altitude) sentences,
drops anything with a bad checksum or no fix, and publishes one frame per RMC to
habitat/eva/gps for position fusion.

Wiring: M9N TX → Pi RX (GPIO15), RX → TX (GPIO14), 3.3 V, GND; enable the UART and
disable the serial console (raspi-config). The M9N defaults to 38400 baud.

Environment:
  GPS_PORT=/dev/serial0  GPS_BAUD=38400  CREW_ID=ev1  + MQTT_*

  --simulate   drifting fix near the default base location (the old behaviour)
"""
import argparse
import json
import logging
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from eva_mqtt import connect, crew_id  # noqa: E402
from hw import env_int, simulate_requested  # noqa: E402
from positioning import parse_nmea  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gps_driver] %(message)s")
log = logging.getLogger(__name__)

TOPIC = "habitat/eva/gps"
# Simulation base location (outdoor analog site near Bengaluru)
BASE_LAT, BASE_LON = 12.9716, 77.5946


class GpsFix:
    """Merge RMC and GGA sentences into publishable frames (pure logic, testable)."""

    def __init__(self, crew: str):
        self.crew = crew
        self.gga = {}

    def feed(self, sentence: str):
        parsed = parse_nmea(sentence)
        if not parsed:
            return None
        if parsed["type"] == "GGA":
            self.gga = parsed
            return None
        frame = {"crew_id": self.crew, "source": "gps",
                 "lat": round(parsed["lat"], 7), "lon": round(parsed["lon"], 7),
                 "speed_kn": parsed["speed_kn"], "timestamp": int(time.time())}
        if self.gga:
            frame.update(satellites=self.gga["satellites"], hdop=self.gga["hdop"], alt_m=self.gga["alt_m"])
        return frame


def sentences_from_serial():
    import serial
    port, baud = os.getenv("GPS_PORT", "/dev/serial0"), env_int("GPS_BAUD", 38400)
    log.info("Reading NMEA from %s @ %d", port, baud)
    with serial.Serial(port, baud, timeout=2) as ser:
        while True:
            line = ser.readline().decode("ascii", "replace").strip()
            if line:
                yield line


def sentences_simulated():
    step = 0
    while True:
        lat = BASE_LAT + step * 0.00001
        lon = BASE_LON + step * 0.00002 + random.uniform(-0.000005, 0.000005)
        dd_lat = int(lat) * 100 + (lat % 1) * 60
        dd_lon = int(lon) * 100 + (lon % 1) * 60
        body = f"GNRMC,123519,A,{dd_lat:.5f},N,{dd_lon:.5f},E,0.4,84.4,250926,,,A"
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        yield f"${body}*{cs:02X}"
        step += 1
        time.sleep(1.0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    fix = GpsFix(crew_id())
    client = connect(f"gps-{fix.crew}")
    source = sentences_simulated() if simulate_requested(args.simulate) else sentences_from_serial()
    log.info("GPS for %s → %s", fix.crew, TOPIC)
    last_log = 0.0
    for sentence in source:
        frame = fix.feed(sentence)
        if frame:
            client.publish(TOPIC, json.dumps(frame))
            if time.monotonic() - last_log > 10:
                log.info("fix %.6f, %.6f (%s sats)", frame["lat"], frame["lon"], frame.get("satellites", "?"))
                last_log = time.monotonic()


if __name__ == "__main__":
    main()
