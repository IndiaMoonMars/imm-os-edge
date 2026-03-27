"""
IMM-OS Sensor Simulator
=======================
Simulates RPi + Jetson edge nodes publishing telemetry to MQTT.

When real sensors arrive, they publish to the same MQTT topics
with the same JSON schema — zero backend changes needed.

Simulated nodes:
  - node-rpi-01  : Habitat Zone A (temp, humidity, pressure, CO2, O2)
  - node-rpi-02  : Habitat Zone B (temp, humidity, pressure, CO2, O2)
  - node-jetson  : Compute / Power (CPU temp, GPU temp, power draw, battery)
"""

import json
import math
import os
import random
import time
import logging

import paho.mqtt.client as mqtt

from config import NODES, MQTT_HOST, MQTT_PORT, PUBLISH_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sensor-sim")


# ── MQTT connection callbacks ─────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
    else:
        log.error("MQTT connect failed with code %s", rc)


def on_disconnect(client, userdata, rc):
    log.warning("Disconnected from MQTT broker (rc=%s). Reconnecting...", rc)


# ── Sensor value generators ───────────────────────────────────────

def sine_wave(t: float, base: float, amplitude: float, period_s: float) -> float:
    """Smooth sinusoidal variation + small Gaussian noise."""
    noise = random.gauss(0, amplitude * 0.05)
    return round(base + amplitude * math.sin(2 * math.pi * t / period_s) + noise, 3)


def random_walk(current: float, step: float, low: float, high: float) -> float:
    """Bounded random walk for realistic sensor drift."""
    new = current + random.uniform(-step, step)
    return round(max(low, min(high, new)), 3)


class SensorState:
    """Tracks per-node mutable state for random-walk sensors."""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self._state: dict[str, float] = {}

    def walk(self, key: str, default: float, step: float, low: float, high: float) -> float:
        self._state[key] = random_walk(
            self._state.get(key, default), step, low, high
        )
        return self._state[key]


# ── Payload builders ──────────────────────────────────────────────

def build_env_payload(node: dict, state: SensorState, t: float) -> dict[str, dict]:
    """Environmental sensor payload for RPi habitat nodes."""
    return {
        "temperature": {
            "value": sine_wave(t, node["temp_base"], 2.0, 3600),
            "unit": "celsius",
        },
        "humidity": {
            "value": sine_wave(t, node["humidity_base"], 5.0, 7200),
            "unit": "percent",
        },
        "pressure": {
            "value": sine_wave(t, node["pressure_base"], 1.5, 5400),
            "unit": "hPa",
        },
        "co2": {
            "value": state.walk("co2", node["co2_base"], 5, 380, 1200),
            "unit": "ppm",
        },
        "o2": {
            "value": state.walk("o2", 20.9, 0.05, 19.5, 21.5),
            "unit": "percent",
        },
    }


def build_power_payload(node: dict, state: SensorState, t: float) -> dict[str, dict]:
    """Power/compute payload for Jetson node."""
    return {
        "cpu_temp": {
            "value": sine_wave(t, node["cpu_temp_base"], 5.0, 1800),
            "unit": "celsius",
        },
        "gpu_temp": {
            "value": sine_wave(t, node["gpu_temp_base"], 8.0, 1800),
            "unit": "celsius",
        },
        "power_draw": {
            "value": state.walk("power", node["power_base"], 1.0, 5.0, 25.0),
            "unit": "watts",
        },
        "battery_level": {
            "value": state.walk("battery", 85.0, 0.1, 20.0, 100.0),
            "unit": "percent",
        },
        "solar_input": {
            "value": max(0, sine_wave(t, 8.0, 8.0, 86400)),  # day/night cycle
            "unit": "watts",
        },
    }


# ── Publish loop ──────────────────────────────────────────────────

def publish_node(client: mqtt.Client, node: dict, state: SensorState, t: float):
    node_id = node["id"]
    ts = int(time.time())

    if node["type"] == "rpi":
        readings = build_env_payload(node, state, t)
    else:
        readings = build_power_payload(node, state, t)

    for measurement, data in readings.items():
        topic = f"imm/habitat/{node_id}/telemetry/{measurement}"
        payload = json.dumps({
            "node_id": node_id,
            "node_type": node["type"],
            "measurement": measurement,
            "value": data["value"],
            "unit": data["unit"],
            "timestamp": ts,
            "simulated": True,  # Flag: remove when real sensor connected
        })
        result = client.publish(topic, payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("Publish failed on %s", topic)

    # Node heartbeat
    client.publish(
        f"imm/habitat/{node_id}/status",
        json.dumps({"node_id": node_id, "status": "online", "timestamp": ts}),
        qos=0,
        retain=True,
    )
    log.debug("Published %d readings for %s", len(readings), node_id)


def main():
    client = mqtt.Client(client_id="imm-sensor-simulator")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    log.info("Connecting to MQTT broker at %s:%s ...", MQTT_HOST, MQTT_PORT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    # Wait for connection
    time.sleep(2)

    states = {node["id"]: SensorState(node["id"]) for node in NODES}
    start = time.time()

    log.info("Simulator started. Publishing every %ss for %d nodes.",
             PUBLISH_INTERVAL_S, len(NODES))

    while True:
        t = time.time() - start
        for node in NODES:
            publish_node(client, node, states[node["id"]], t)
        time.sleep(PUBLISH_INTERVAL_S)


if __name__ == "__main__":
    main()
