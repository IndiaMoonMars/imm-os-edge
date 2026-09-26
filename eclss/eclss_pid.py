#!/usr/bin/env python3
"""
ECLSS climate controller — HVAC (cooling) and dehumidifier relays.

Temperature and humidity come from this node's own sensor readings over MQTT
(habitat/sensors/<bme280|scd40>/<zone>, filtered by node_id), so the I2C sensors
keep a single owner (their driver). Each load is switched with hysteresis around
its setpoint plus minimum on/off times (compressors must not short-cycle).

Fail-safe: relays are de-energised (OFF) at start-up, on exit, and whenever no
fresh reading has arrived for CLIMATE_STALE_S seconds.

Environment:
  HVAC_RELAY_GPIO=23  DEHUM_RELAY_GPIO=24  RELAY_ACTIVE_LOW=true
  TEMP_SETPOINT_C=22  TEMP_BAND_C=1.0      → cool above 23 °C, stop below 21 °C
  HUM_SETPOINT_PCT=50 HUM_BAND_PCT=5       → dehumidify above 55 %, stop below 45 %
  MIN_ON_S=180  MIN_OFF_S=180  CLIMATE_STALE_S=120
  CLIMATE_NODE_ID (default IMM_NODE_ID) and the MQTT_* settings

  --simulate   simulated room + logged relays, no hardware or MQTT (the old demo)
"""
import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from hw import LogRelay, Relay, env_float, env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [eclss_climate] %(message)s")
log = logging.getLogger(__name__)


class Hysteresis:
    """
    On/off control for a load that pushes the value DOWN (cooler, drier):
    switch on above setpoint+band, off below setpoint−band, respecting minimum
    on/off times. Pure logic, testable.
    """

    def __init__(self, setpoint: float, band: float, min_on_s: float, min_off_s: float):
        self.setpoint, self.band = setpoint, band
        self.min_on_s, self.min_off_s = min_on_s, min_off_s
        self.on = False
        self.changed_at = -1e9

    def decide(self, value: float, now: float) -> bool:
        held = now - self.changed_at
        if not self.on and value > self.setpoint + self.band and held >= self.min_off_s:
            self.on, self.changed_at = True, now
        elif self.on and value < self.setpoint - self.band and held >= self.min_on_s:
            self.on, self.changed_at = False, now
        return self.on

    def force_off(self, now: float) -> None:
        if self.on:
            self.on, self.changed_at = False, now


class LatestReadings:
    """Newest temperature/humidity for one node from sensor MQTT messages."""
    PREFERENCE = {"bme280": 0, "scd40": 1}

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.temp = self.hum = None
        self.at = 0.0
        self._src = {}
        self._lock = threading.Lock()

    def feed(self, payload: dict, now: float) -> None:
        sensor = payload.get("sensor")
        if payload.get("node_id") != self.node_id or sensor not in self.PREFERENCE:
            return
        with self._lock:
            for field, attr in (("temp", "temp"), ("hum", "hum")):
                v = payload.get(field)
                if v is None:
                    continue
                cur = self._src.get(attr)
                # keep the preferred sensor unless it has gone quiet for a minute
                if cur is None or self.PREFERENCE[sensor] <= self.PREFERENCE[cur[0]] or now - cur[1] > 60:
                    setattr(self, attr, float(v))
                    self._src[attr] = (sensor, now)
            self.at = now

    def snapshot(self):
        with self._lock:
            return self.temp, self.hum, self.at


def subscribe_readings(latest: LatestReadings):
    import paho.mqtt.client as mqtt
    host, port = os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883"))

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe([("habitat/sensors/bme280/+", 0), ("habitat/sensors/scd40/+", 0)])
            log.info("Following %s's temperature/humidity via MQTT %s:%s", latest.node_id, host, port)
        else:
            log.error("MQTT connect failed rc=%s", rc)

    def on_message(client, userdata, msg):
        try:
            latest.feed(json.loads(msg.payload), time.monotonic())
        except (ValueError, TypeError):
            pass

    client = mqtt.Client(client_id=f"imm-climate-{latest.node_id}")
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(host, port, 60)
    client.loop_start()
    return client


def control_loop(get_values, hvac, dehum, temp_ctl, hum_ctl, stale_s, period_s, stop):
    stale_logged = False
    while not stop.is_set():
        now = time.monotonic()
        temp, hum, at = get_values()
        if temp is None or now - at > stale_s:
            if not stale_logged:
                log.warning("No fresh temperature/humidity for %.0f s: relays OFF (fail-safe)", stale_s)
                stale_logged = True
            temp_ctl.force_off(now)
            hum_ctl.force_off(now)
        else:
            stale_logged = False
            temp_ctl.decide(temp, now)
            if hum is not None:
                hum_ctl.decide(hum, now)
            log.info("%.2f °C (set %.1f) | %s %% (set %.0f) | HVAC %s | dehumidifier %s",
                     temp, temp_ctl.setpoint, f"{hum:.1f}" if hum is not None else "n/a", hum_ctl.setpoint,
                     "ON" if temp_ctl.on else "off", "ON" if hum_ctl.on else "off")
        hvac.set(temp_ctl.on)
        dehum.set(hum_ctl.on)
        stop.wait(period_s)


class SimulatedRoom:
    """The old demo: HVAC cools, the room warms; the dehumidifier dries, the room gets humid."""

    def __init__(self, temp_ctl, hum_ctl):
        self.temp, self.hum = 22.0, 50.0
        self.temp_ctl, self.hum_ctl = temp_ctl, hum_ctl

    def values(self):
        self.temp += (-0.6 if self.temp_ctl.on else 0.2) + random.uniform(-0.5, 0.5)
        self.hum += (-2.0 if self.hum_ctl.on else 0.5) + random.uniform(-1.0, 1.0)
        return self.temp, self.hum, time.monotonic()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    simulate = simulate_requested(args.simulate)

    min_on, min_off = env_float("MIN_ON_S", 180), env_float("MIN_OFF_S", 180)
    if simulate:
        min_on = min_off = 0.0
    temp_ctl = Hysteresis(env_float("TEMP_SETPOINT_C", 22), env_float("TEMP_BAND_C", 1.0), min_on, min_off)
    hum_ctl = Hysteresis(env_float("HUM_SETPOINT_PCT", 50), env_float("HUM_BAND_PCT", 5), min_on, min_off)

    if simulate:
        hvac, dehum = LogRelay("HVAC"), LogRelay("Dehumidifier")
        get_values, period = SimulatedRoom(temp_ctl, hum_ctl).values, 2.0
    else:
        hvac = Relay("HVAC", env_int("HVAC_RELAY_GPIO", 23))
        dehum = Relay("Dehumidifier", env_int("DEHUM_RELAY_GPIO", 24))
        node = os.getenv("CLIMATE_NODE_ID") or os.getenv("IMM_NODE_ID")
        if not node:
            raise SystemExit("Set IMM_NODE_ID (or CLIMATE_NODE_ID) to the node whose sensors drive this controller")
        latest = LatestReadings(node)
        subscribe_readings(latest)
        get_values, period = latest.snapshot, 5.0

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    log.info("Climate controller started (%s)", "simulated" if simulate else "relays live")
    try:
        control_loop(get_values, hvac, dehum, temp_ctl, hum_ctl, env_float("CLIMATE_STALE_S", 120), period, stop)
    finally:
        hvac.close()
        dehum.close()
        log.info("Relays OFF, controller stopped")


if __name__ == "__main__":
    main()
