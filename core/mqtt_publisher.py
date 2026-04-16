#!/usr/bin/env python3
"""
Shared MQTT publisher helper for IMM-OS sensor drivers.
Used in --mode mqtt to publish JSON payloads to the broker.
"""

import os
import json
import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_QOS = 1


def create_client() -> mqtt.Client:
    client = mqtt.Client(client_id="", clean_session=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


def publish(client: mqtt.Client, topic: str, payload: dict) -> None:
    """Serialize payload to JSON and publish to the given MQTT topic."""
    msg = json.dumps(payload, separators=(",", ":"))
    result = client.publish(topic, msg, qos=MQTT_QOS)
    result.wait_for_publish(timeout=2.0)
