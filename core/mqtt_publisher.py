#!/usr/bin/env python3
"""
Shared MQTT publisher helper for IMM-OS sensor drivers.

Drivers call make_publisher(mode, topic) and then publish_fn(payload[, topic]):
  --mode stdout  JSON lines on stdout only (pipe into encryption_layer → blackbox_logger)
  --mode mqtt    publish to the broker only
  --mode both    publish to the broker AND keep the stdout stream for the blackbox
                 (what the systemd units use)

Every reading is stamped with this node's identity before it leaves the node:
  node_id    IMM_NODE_ID (default: hostname)
  zone       the payload's own zone, else IMM_ZONE, else the topic's last segment
  simulated  false (real hardware)
"""

import os
import json
import socket
import sys
import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_QOS = 1


def create_client() -> mqtt.Client:
    client = mqtt.Client(client_id="", clean_session=True)
    # Broker requires auth (allow_anonymous false); user/topics in imm-os-infra mosquitto/config/acl
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):  # broker TLS listener (8883); verifies cert + hostname
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


def publish(client: mqtt.Client, topic: str, payload: dict) -> None:
    """Serialize payload to JSON and publish to the given MQTT topic."""
    msg = json.dumps(payload, separators=(",", ":"))
    result = client.publish(topic, msg, qos=MQTT_QOS)
    result.wait_for_publish(timeout=2.0)


def stamp(payload: dict, topic: str) -> tuple:
    """Add node_id / zone / simulated and return (payload, topic) with IMM_ZONE applied."""
    out = dict(payload)
    out.setdefault("node_id", os.getenv("IMM_NODE_ID") or socket.gethostname())
    out.setdefault("simulated", False)
    parts = topic.split("/")
    zone_env = os.getenv("IMM_ZONE")
    if "zone" not in out and zone_env and len(parts) == 4:
        parts[3] = zone_env
        topic = "/".join(parts)
    if "zone" not in out and len(parts) == 4:
        out["zone"] = parts[3]
    return out, topic


def make_publisher(mode: str, default_topic: str = None):
    """Return publish_fn(payload, topic=None) for --mode stdout | mqtt | both."""
    client = create_client() if mode in ("mqtt", "both") else None

    def publish_fn(payload: dict, topic: str = None) -> None:
        msg, t = stamp(payload, topic or default_topic)
        if client is not None:
            publish(client, t, msg)
        if mode in ("stdout", "both"):
            print(json.dumps(msg), flush=True)

    return publish_fn


MODES = ("stdout", "mqtt", "both")
