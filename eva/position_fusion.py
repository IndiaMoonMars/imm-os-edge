#!/usr/bin/env python3
"""
Position Fusion — Merges UWB (indoor) and GPS (outdoor) streams.

Decision logic:
  - Uses UWB exclusively if quality >= UWB_QUALITY_THRESHOLD (strong beacon fix).
  - Falls back to GPS when quality drops (crew leaving habitat airlock).
  - Publishes unified frame to: habitat/eva/position/{crew_id}
"""
import json
import time
import logging
import threading
import paho.mqtt.client as mqtt
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pos_fusion] %(message)s")
log = logging.getLogger(__name__)

MQTT_HOST           = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT           = int(os.getenv("MQTT_PORT", 1883))
UWB_QUALITY_THRESH  = int(os.getenv("UWB_QUALITY_THRESH", 70))   # below → switch to GPS

# Latest frames per crew member
_uwb_frames: dict[str, dict] = {}
_gps_frames: dict[str, dict] = {}
_lock = threading.Lock()

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        crew_id = data.get("crew_id", "EV1")
        with _lock:
            if "uwb" in msg.topic:
                _uwb_frames[crew_id] = data
            elif "gps" in msg.topic:
                _gps_frames[crew_id] = data
    except Exception as e:
        log.error(f"Parse error: {e}")

def fuse_and_publish(pub_client: mqtt.Client):
    """Every 200 ms pick the best source and publish the unified position."""
    while True:
        with _lock:
            all_crew = set(_uwb_frames) | set(_gps_frames)
        for crew_id in all_crew:
            with _lock:
                uwb = _uwb_frames.get(crew_id)
                gps = _gps_frames.get(crew_id)

            if uwb and uwb.get("quality", 0) >= UWB_QUALITY_THRESH:
                unified = {
                    "crew_id": crew_id,
                    "mode": "uwb",
                    "x_m": uwb["x_m"],
                    "y_m": uwb["y_m"],
                    "z_m": uwb.get("z_m", 0),
                    "quality": uwb["quality"],
                    "timestamp": int(time.time())
                }
            elif gps:
                unified = {
                    "crew_id": crew_id,
                    "mode": "gps",
                    "lat": gps["lat"],
                    "lon": gps["lon"],
                    "quality": 50,           # GPS fallback quality proxy
                    "timestamp": int(time.time())
                }
            else:
                continue

            topic = f"habitat/eva/position/{crew_id}"
            pub_client.publish(topic, json.dumps(unified))
            log.info(f"[{crew_id}] mode={unified['mode']} quality={unified['quality']}")

        time.sleep(0.2)

def main():
    client = mqtt.Client(client_id="pos-fusion")
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe("habitat/eva/uwb")
    client.subscribe("habitat/eva/gps")
    client.loop_start()

    log.info("Position Fusion daemon online.")
    fuse_and_publish(client)       # blocks; threadsafe publishing loop

if __name__ == "__main__":
    main()
