#!/usr/bin/env python3
"""
UWB Driver — Interfaces with Decawave DWM1001 beacons over serial UART.
Parses position packets from the DWM1001 TAG in simulation mode generating
X/Y coordinates in a 10m × 10m habitat grid at >5 Hz.
"""
import time
import json
import logging
import random
import math
import paho.mqtt.client as mqtt
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [uwb_driver] %(message)s")
log = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))

# Habitat grid bounds (meters)
GRID_W = 10.0
GRID_H = 10.0

def sim_uwb_position(step: int):
    """Simulate a crew member walking a deterministic arc around the habitat."""
    t = step * 0.2                          # 5 Hz → 0.2 s step
    radius = 3.5
    cx, cy = GRID_W / 2, GRID_H / 2
    x = round(cx + radius * math.cos(t * 0.3) + random.uniform(-0.03, 0.03), 3)
    y = round(cy + radius * math.sin(t * 0.3) + random.uniform(-0.03, 0.03), 3)
    z = round(1.2 + random.uniform(-0.02, 0.02), 3)  # standing height ≈ 1.2 m
    return x, y, z

def main():
    client = mqtt.Client(client_id="uwb-driver")
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    log.info("UWB Driver online. Publishing to habitat/eva/uwb at 5 Hz.")

    step = 0
    while True:
        x, y, z = sim_uwb_position(step)
        quality = random.randint(85, 100)      # dilution-of-precision proxy
        payload = json.dumps({
            "crew_id": "EV1",
            "source": "uwb",
            "x_m": x,
            "y_m": y,
            "z_m": z,
            "quality": quality,
            "timestamp": int(time.time())
        })
        client.publish("habitat/eva/uwb", payload)
        log.debug(f"UWB  x={x} y={y} q={quality}")
        step += 1
        time.sleep(0.2)  # 5 Hz

if __name__ == "__main__":
    main()
