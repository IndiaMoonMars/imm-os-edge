"""
IMM-OS Sensor Simulator
=======================
Simulates the Raspberry Pi edge nodes, publishing exactly what the real drivers in
sensor_drivers/ publish: one JSON reading per sensor on
habitat/sensors/<sensor>/<zone>, stamped with node_id, zone and "simulated": true.
The telemetry pipeline (bridge → validator → processor) treats both the same.

Simulated nodes and sensors:
  - node-rpi-01   zone_a : bme280, scd40, o2, sysmon
  - node-rpi-02   zone_b : bme280, scd40, o2, sysmon
  - node-compute  compute: sysmon (Raspberry Pi 5 health + PMIC power), bms (battery, solar)

Bring real hardware online one sensor at a time with DISABLED_SENSORS, a comma
list of "sensor" (all nodes) or "node_id:sensor" entries, e.g.
  DISABLED_SENSORS=node-rpi-01:bme280,node-rpi-01:scd40
Set SIM_LEGACY_TOPICS=true to also publish the old imm/habitat/<node>/telemetry/* format.
"""

import json
import math
import os
import random
import time
import logging

import paho.mqtt.client as mqtt

from config import NODES, MQTT_HOST, MQTT_PORT, PUBLISH_INTERVAL_S

LEGACY_TOPICS = os.getenv("SIM_LEGACY_TOPICS", "false").lower() == "true"

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


def build_sysmon_payload(node: dict, state: SensorState, t: float) -> dict[str, dict]:
    """Node health (sysmon_driver.py) for every node; PMIC power only on a Pi 5."""
    out = {
        "cpu_temp": {"value": sine_wave(t, node.get("cpu_temp_base", 47.0), 4.0, 1800), "unit": "celsius"},
        "cpu_load": {"value": state.walk("load", 12.0, 3.0, 2.0, 95.0), "unit": "percent"},
        "mem_pct": {"value": state.walk("mem", 35.0, 0.5, 10.0, 90.0), "unit": "percent"},
        "disk_pct": {"value": state.walk("disk", 22.0, 0.01, 5.0, 95.0), "unit": "percent"},
        "undervolt": {"value": 0, "unit": "flag"},
        "throttled": {"value": 0, "unit": "flag"},
    }
    if "Pi 5" in node.get("hardware", ""):
        out["fan_rpm"] = {"value": int(state.walk("fan", 2600, 80, 0, 8000)), "unit": "rpm"}
        out["supply_v"] = {"value": state.walk("supply", 5.1, 0.01, 4.9, 5.25), "unit": "volts"}
    return out


def build_power_payload(node: dict, state: SensorState, t: float) -> dict[str, dict]:
    """Compute/power node: Pi 5 board power + battery and solar."""
    return {
        "power_draw": {
            "value": state.walk("power", node["power_base"], 0.4, 2.5, 12.0),
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


# ── Sensor-format readings (same schema as the real drivers) ─────

SYSMON_METRICS = ("cpu_temp", "cpu_load", "mem_pct", "disk_pct", "fan_rpm", "supply_v", "undervolt", "throttled")


def parse_disabled(spec: str) -> set:
    return {x.strip() for x in (spec or "").split(",") if x.strip()}


def is_disabled(disabled: set, node_id: str, sensor: str) -> bool:
    return sensor in disabled or f"{node_id}:{sensor}" in disabled


def build_sensor_readings(node: dict, env: dict, ts: int) -> list:
    """Map one tick of simulated values to [(topic, payload)] in driver format."""
    zone = node["zone"]
    v = {k: env[k]["value"] for k in env}
    if node["type"] == "rpi":
        readings = [
            ("bme280", {"temp": v["temperature"], "hum": v["humidity"], "pres": v["pressure"]}),
            ("scd40", {"co2_ppm": v["co2"], "temp": round(v["temperature"] + 0.4, 3), "hum": round(v["humidity"] - 1.0, 3)}),
            ("o2", {"o2_pct": v["o2"]}),
        ]
    else:
        readings = [("bms", {"battery_pct": v["battery_level"], "solar_w": v["solar_input"]})]
    sysmon = {k: v[k] for k in SYSMON_METRICS if k in v}
    if "power_draw" in v:
        sysmon["power_w"] = v["power_draw"]
    if sysmon:
        readings.append(("sysmon", sysmon))
    out = []
    for sensor, metrics in readings:
        payload = {"sensor": sensor, **metrics, "timestamp": ts,
                   "node_id": node["id"], "zone": zone, "simulated": True}
        out.append((f"habitat/sensors/{sensor}/{zone}", payload))
    return out


# ── Publish loop ──────────────────────────────────────────────────

def publish_node(client: mqtt.Client, node: dict, state: SensorState, t: float, disabled: set):
    node_id = node["id"]
    ts = int(time.time())

    if node["type"] == "rpi":
        readings = build_env_payload(node, state, t)
    else:
        readings = build_power_payload(node, state, t)
    readings.update(build_sysmon_payload(node, state, t))

    sent = 0
    for topic, payload in build_sensor_readings(node, readings, ts):
        if is_disabled(disabled, node_id, payload["sensor"]):
            continue
        result = client.publish(topic, json.dumps(payload), qos=1)
        sent += 1
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("Publish failed on %s", topic)

    if LEGACY_TOPICS:
        for measurement, data in readings.items():
            client.publish(f"imm/habitat/{node_id}/telemetry/{measurement}", json.dumps({
                "node_id": node_id, "node_type": node["type"], "measurement": measurement,
                "value": data["value"], "unit": data["unit"], "timestamp": ts, "simulated": True,
            }), qos=1)

    # Node heartbeat
    client.publish(
        f"imm/habitat/{node_id}/status",
        json.dumps({"node_id": node_id, "status": "online", "simulated": True, "timestamp": ts}),
        qos=0,
        retain=True,
    )
    log.debug("Published %d sensor readings for %s", sent, node_id)


def main():
    client = mqtt.Client(client_id="imm-sensor-simulator")
    # Broker requires auth (allow_anonymous false); user/topics in imm-os-infra mosquitto/config/acl
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):  # broker TLS listener (8883); verifies cert + hostname
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    log.info("Connecting to MQTT broker at %s:%s ...", MQTT_HOST, MQTT_PORT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    # Wait for connection
    time.sleep(2)

    states = {node["id"]: SensorState(node["id"]) for node in NODES}
    disabled = parse_disabled(os.getenv("DISABLED_SENSORS", ""))
    if disabled:
        log.info("Not simulating (real hardware online): %s", ", ".join(sorted(disabled)))
    start = time.time()

    log.info("Simulator started. Publishing every %ss for %d nodes.",
             PUBLISH_INTERVAL_S, len(NODES))

    while True:
        t = time.time() - start
        for node in NODES:
            publish_node(client, node, states[node["id"]], t, disabled)
        time.sleep(PUBLISH_INTERVAL_S)


if __name__ == "__main__":
    main()
