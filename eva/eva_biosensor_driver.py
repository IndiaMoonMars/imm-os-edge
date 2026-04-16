#!/usr/bin/env python3
"""
EVA Biosensor Driver — Streams spacesuit vitals at 5 Hz.
Sensors modelled:
  - MAX30100  : HR (BPM) + SpO₂ (%)
  - MLX90614  : Non-contact skin temperature (°C)
  - AD8232    : ECG voltage (mV) — single-lead simplified waveform

In simulation mode the values follow realistic baseline with controllable
stress injection via ENV var EVA_STRESS_MODE=true (raises HR to >160).
"""
import json
import math
import os
import random
import time
import logging
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [eva_bio] %(message)s")
log = logging.getLogger(__name__)

MQTT_HOST   = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))
CREW_ID     = os.getenv("CREW_ID", "EV1")
STRESS_MODE = os.getenv("EVA_STRESS_MODE", "false").lower() == "true"

def sim_hr(t: float) -> float:
    base = 162.0 if STRESS_MODE else 72.0
    return round(base + 5 * math.sin(t * 0.1) + random.uniform(-2, 2), 1)

def sim_spo2(t: float) -> float:
    base = 92.5 if STRESS_MODE else 98.2
    return round(base + random.uniform(-0.3, 0.3), 1)

def sim_skin_temp(t: float) -> float:
    base = 38.8 if STRESS_MODE else 36.6
    return round(base + 0.1 * math.sin(t * 0.05) + random.uniform(-0.05, 0.05), 2)

def sim_ecg(t: float) -> float:
    """Simplified PQRST-shape ECG waveform in mV."""
    phase = (t * 1.2) % (2 * math.pi)
    if phase < 0.1:
        return round(0.25 * math.sin(phase * 31), 3)  # P-wave
    elif phase < 0.25:
        return round(-0.1 * math.sin((phase - 0.1) * 21), 3)  # Q
    elif phase < 0.35:
        return round(1.2 * math.sin((phase - 0.25) * 31), 3)  # R (spike)
    elif phase < 0.50:
        return round(-0.15 * math.sin((phase - 0.35) * 21), 3)  # S
    elif phase < 1.0:
        return round(0.3 * math.sin((phase - 0.5) * 6.28), 3)  # T-wave
    return 0.0

def main():
    client = mqtt.Client(client_id=f"eva-bio-{CREW_ID}")
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    log.info(f"EVA Biosensor Driver online for {CREW_ID} (stress={STRESS_MODE}). Publishing at 5 Hz.")

    t = 0.0
    while True:
        payload = json.dumps({
            "crew_id": CREW_ID,
            "sensor": "eva_biosensor",
            "hr_bpm": sim_hr(t),
            "spo2_pct": sim_spo2(t),
            "skin_temp_c": sim_skin_temp(t),
            "ecg_mv": sim_ecg(t),
            "timestamp": int(time.time())
        })
        client.publish(f"habitat/eva/biosensors/{CREW_ID}", payload)
        t += 0.2
        time.sleep(0.2)

if __name__ == "__main__":
    main()
