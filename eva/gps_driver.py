#!/usr/bin/env python3
"""
GPS Driver — Parses NMEA GPRMC strings from u-blox NEO-M9N via UART.
In simulation mode, generates synthetic lat/lon that slowly drifts (mock outdoor EVA traversal).
"""
import time
import math
import json
import logging
import random
import paho.mqtt.client as mqtt
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gps_driver] %(message)s")
log = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))

# Base location: simulate outdoor habitat lat/lon near Bengaluru
BASE_LAT = 12.9716
BASE_LON = 77.5946

def parse_gprmc(sentence: str):
    """Parse NMEA GPRMC sentence into lat/lon. Returns (lat, lon) or None."""
    parts = sentence.split(',')
    if len(parts) < 7 or parts[2] != 'A':
        return None
    try:
        raw_lat = float(parts[3]); lat_dir = parts[4]
        raw_lon = float(parts[5]); lon_dir = parts[6]
        lat = int(raw_lat / 100) + (raw_lat % 100) / 60.0
        lon = int(raw_lon / 100) + (raw_lon % 100) / 60.0
        if lat_dir == 'S': lat = -lat
        if lon_dir == 'W': lon = -lon
        return lat, lon
    except Exception:
        return None

def sim_gprmc(step: int):
    """Generate a drifting NMEA GPRMC sentence for mock outdoor EVA traversal."""
    drift_lat = BASE_LAT + step * 0.00001
    drift_lon = BASE_LON + step * 0.00002 + random.uniform(-0.000005, 0.000005)
    dd_lat = int(drift_lat) * 100 + (drift_lat % 1) * 60
    dd_lon = int(drift_lon) * 100 + (drift_lon % 1) * 60
    return f"$GPRMC,123519,A,{dd_lat:.4f},N,{dd_lon:.4f},E,022.4,084.4,230394,003.1,W*6A"

def main():
    client = mqtt.Client(client_id="gps-driver")
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    log.info("GPS Driver online. Publishing to habitat/eva/gps at 1 Hz.")

    step = 0
    while True:
        sentence = sim_gprmc(step)
        result = parse_gprmc(sentence)
        if result:
            lat, lon = result
            payload = json.dumps({
                "crew_id": "EV1",
                "source": "gps",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "timestamp": int(time.time())
            })
            client.publish("habitat/eva/gps", payload)
            log.info(f"GPS  lat={lat:.6f} lon={lon:.6f}")
        step += 1
        time.sleep(1.0)

if __name__ == "__main__":
    main()
