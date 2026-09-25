#!/usr/bin/env python3
"""
EVA biosensor driver — suit vitals streamed at 5 Hz to habitat/eva/biosensors/<crew>.

Sensors (all I2C except the ECG front end):
  MAX30100 / MAX30102  PPG at 100 Hz → heart rate and SpO2 over a 5 s window
                       (core/biometrics.py); omitted while there is no good contact
  MLX90614             non-contact skin temperature, object channel (0x5A)
  AD8232 + ADS1115     single-lead ECG, instantaneous mV (ADS1115 A0, 0x48)

Each sensor is optional: one that fails to initialise is logged and left out of
the frames, so a broken ECG lead doesn't stop HR/SpO2 from reaching MCC.

Environment:
  CREW_ID=ev1  MLX90614_ADDRESS=0x5A  ECG_ADS_ADDRESS=0x48  I2C_BUS=1  + MQTT_*

  --simulate   synthetic vitals; EVA_STRESS_MODE=true raises HR above 160 (old behaviour)
"""
import argparse
import collections
import json
import logging
import math
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from biometrics import heart_rate, spo2  # noqa: E402
from calibration import default as calibration  # noqa: E402
from eva_mqtt import connect, crew_id  # noqa: E402
from hw import env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [eva_bio] %(message)s")
log = logging.getLogger(__name__)

PPG_HZ = 100
WINDOW_S = 5
STRESS_MODE = os.getenv("EVA_STRESS_MODE", "false").lower() == "true"


# ── Hardware ──────────────────────────────────────────────────────

class PpgSampler(threading.Thread):
    """Reads MAX30100 red/IR at ~100 Hz into a rolling window; recomputes HR/SpO2 each second."""

    def __init__(self):
        super().__init__(daemon=True)
        from max30100 import MAX30100   # core/max30100.py
        self.dev = MAX30100(bus=env_int("I2C_BUS", 1))
        self.dev.enable_spo2()
        n = PPG_HZ * WINDOW_S
        self.ir, self.red = collections.deque(maxlen=n), collections.deque(maxlen=n)
        self.hr = self.spo2 = None
        self._lock = threading.Lock()

    def run(self):
        next_calc = time.monotonic() + 1
        while True:
            try:
                for ir, red in self.dev.read_fifo():   # the chip samples at exactly 100 Hz
                    self.ir.append(ir)
                    self.red.append(red)
            except OSError as exc:
                log.warning("MAX30100 read error: %s", exc)
                time.sleep(0.5)
            now = time.monotonic()
            if now >= next_calc and len(self.ir) == self.ir.maxlen:
                ir, red = list(self.ir), list(self.red)
                hr, ox = heart_rate(ir, PPG_HZ), spo2(red, ir)
                with self._lock:
                    self.hr, self.spo2 = hr, ox
                next_calc = now + 1
            time.sleep(0.05)      # FIFO holds 16 samples (160 ms); poll well inside that

    def latest(self):
        with self._lock:
            return self.hr, self.spo2


class SkinTemp:
    def __init__(self):
        from smbus2 import SMBus
        self.bus = SMBus(env_int("I2C_BUS", 1))
        self.addr = env_int("MLX90614_ADDRESS", 0x5A)
        self.read()  # fail now if absent

    def read(self):
        raw = self.bus.read_word_data(self.addr, 0x07)   # Tobj1 RAM register, 0.02 K/LSB
        if raw & 0x8000:                                  # error flag
            return None
        return round(raw * 0.02 - 273.15, 2)


class Ecg:
    def __init__(self):
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        ads = ADS.ADS1115(busio.I2C(board.SCL, board.SDA), address=env_int("ECG_ADS_ADDRESS", 0x48))
        ads.data_rate = 860
        self.chan = AnalogIn(ads, ADS.P0)

    def read(self):
        return round(self.chan.voltage * 1000.0, 1)   # mV (AD8232 output, 1.65 V mid-rail)


def optional(name, factory):
    try:
        dev = factory()
        log.info("%s online", name)
        return dev
    except Exception as exc:  # missing library, I2C NACK, no device
        log.error("%s unavailable (%s); its values will be omitted", name, exc)
        return None


# ── Simulation (old behaviour) ────────────────────────────────────

def sim_frame(t: float) -> dict:
    base_hr, base_spo2, base_temp = (162.0, 92.5, 38.8) if STRESS_MODE else (72.0, 98.2, 36.6)
    phase = (t * 1.2) % (2 * math.pi)
    ecg = 1.2 * math.sin((phase - 0.25) * 31) if 0.25 <= phase < 0.35 else 0.3 * math.sin((phase - 0.5) * 6.28) if 0.5 <= phase < 1.0 else 0.0
    return {"hr_bpm": round(base_hr + 5 * math.sin(t * 0.1) + random.uniform(-2, 2), 1),
            "spo2_pct": round(base_spo2 + random.uniform(-0.3, 0.3), 1),
            "skin_temp_c": round(base_temp + 0.1 * math.sin(t * 0.05) + random.uniform(-0.05, 0.05), 2),
            "ecg_mv": round(ecg, 3)}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    simulate = simulate_requested(args.simulate)
    crew = crew_id()
    topic = f"habitat/eva/biosensors/{crew}"
    client = connect(f"eva-bio-{crew}")

    ppg = skin = ecg = None
    if not simulate:
        ppg = optional("MAX30100 (HR/SpO2)", PpgSampler)
        if ppg:
            ppg.start()
        skin = optional("MLX90614 (skin temperature)", SkinTemp)
        ecg = optional("AD8232 ECG", Ecg)
        if not (ppg or skin or ecg):
            raise SystemExit("No EVA biosensors found; check I2C wiring (i2cdetect -y 1) or use --simulate")

    log.info("EVA biosensors for %s → %s at 5 Hz (%s)", crew, topic, "simulated" if simulate else "hardware")
    t, skin_t, last_skin = 0.0, 0.0, None
    while True:
        frame = {"crew_id": crew, "sensor": "eva_biosensor", "simulated": simulate, "timestamp": int(time.time())}
        if simulate:
            frame.update(sim_frame(t))
        else:
            if ppg:
                hr, ox = ppg.latest()
                if hr is not None:
                    frame["hr_bpm"] = hr
                if ox is not None:
                    frame["spo2_pct"] = ox
            if skin and time.monotonic() - skin_t >= 1.0:   # skin temp changes slowly
                try:
                    last_skin = skin.read()
                    if last_skin is not None:
                        last_skin = calibration().correct("mlx90614", "temp", last_skin)
                except OSError as exc:
                    log.warning("MLX90614 read error: %s", exc)
                    last_skin = None
                skin_t = time.monotonic()
            if last_skin is not None:
                frame["skin_temp_c"] = last_skin
            if ecg:
                try:
                    frame["ecg_mv"] = ecg.read()
                except OSError:
                    pass
        if any(k in frame for k in ("hr_bpm", "spo2_pct", "skin_temp_c", "ecg_mv")):
            client.publish(topic, json.dumps(frame))
        t += 0.2
        time.sleep(0.2)


if __name__ == "__main__":
    main()
