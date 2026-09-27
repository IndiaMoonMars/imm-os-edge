#!/usr/bin/env python3
"""
Check every value of the ESP32 sensor board, sensor by sensor, on the Pi it is plugged into.

  sudo .venv/bin/python tools/verify_esp32.py            # all values, automatic checks, hands-on tests
  sudo .venv/bin/python tools/verify_esp32.py --auto     # all values and automatic checks only
  sudo .venv/bin/python tools/verify_esp32.py --seconds 60

1. Every value the board sends is shown (latest, min, max) for --seconds (default 30).
2. Automatic checks. Each value against its physical range, and the sensors against each other:
   - the BME280's and SCD40's dew points (same air, same dew point: both humidity sensors);
   - their temperatures, and the BNO055's own chip temperature;
   - O₂ against CO₂ (air that has lost O₂ has gained about as much CO₂);
   - gravity 9.8 m/s² and the Earth's magnetic field (25-65 µT) at the BNO055;
   - the BNO055's power-on self-test, the MQ-4's signal.
3. Hands-on tests (skip any with s): breathe on the board (CO₂, humidity, O₂), warm the
   BME280 with your hand, tilt and turn the board (BNO055), gas from an unlit lighter (MQ-4).
   Each watches the values live and passes as soon as the sensor responds.

The esp32_bridge service holds the board's USB port: it is stopped while this runs and started
again afterwards. Exit status: 0 no FAIL, 1 something failed, 2 no board.
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "sensor_drivers"))
from esp32_bridge import dew_point, open_port, parse_line  # noqa: E402
from hw import esp32_port  # noqa: E402

SERVICE = "imm-sensor-pipeline@esp32_bridge.py"

# (sensor, field) → (label, unit, decimals); the order in which the table lists them
FIELDS: Dict[Tuple[str, str], Tuple[str, str, int]] = {
    ("bme280", "temp"): ("Temperature", "°C", 2),
    ("bme280", "hum"): ("Humidity", "%RH", 1),
    ("bme280", "pres"): ("Pressure", "hPa", 1),
    ("bme280", "dew"): ("Dew point (calculated)", "°C", 1),
    ("scd40", "co2_ppm"): ("CO₂", "ppm", 0),
    ("scd40", "temp"): ("Temperature", "°C", 2),
    ("scd40", "hum"): ("Humidity", "%RH", 1),
    ("scd40", "dew"): ("Dew point (calculated)", "°C", 1),
    ("o2", "o2_pct"): ("O₂", "%", 2),
    ("bno055", "heading_deg"): ("Heading", "°", 1),
    ("bno055", "roll_deg"): ("Roll", "°", 1),
    ("bno055", "pitch_deg"): ("Pitch", "°", 1),
    ("bno055", "grav_ms2"): ("Gravity", "m/s²", 2),
    ("bno055", "lin_acc_ms2"): ("Motion (no gravity)", "m/s²", 2),
    ("bno055", "gyro_dps"): ("Rotation rate", "°/s", 2),
    ("bno055", "mag_ut"): ("Magnetic field", "µT", 1),
    ("bno055", "temp"): ("Chip temperature", "°C", 0),
    ("bno055", "imu_calib"): ("Calibration: system", "/3", 0),
    ("bno055", "calib_gyro"): ("Calibration: gyro", "/3", 0),
    ("bno055", "calib_acc"): ("Calibration: accelerometer", "/3", 0),
    ("bno055", "calib_mag"): ("Calibration: magnetometer", "/3", 0),
    ("mq4", "vout_mv"): ("Sensor output", "mV", 0),
    ("mq4", "rs_rl"): ("Rs/RL", "×", 3),
    ("mq4", "rs_r0"): ("Rs/R0 (after CAL_MQ4)", "×", 3),
    ("mq4", "ch4_ppm"): ("CH₄ (after CAL_MQ4)", "ppm", 1),
    ("mq4", "warming"): ("Warming up", "", 0),
    ("mq4", "calibrated"): ("Calibrated", "", 0),
}
SENSORS = {"bme280": "BME280 temperature / humidity / pressure", "scd40": "SCD40 CO₂",
           "o2": "SEN0322 oxygen", "bno055": "BNO055 orientation", "mq4": "MQ-4 methane"}

Sample = Tuple[float, dict]          # (time, one board line)


# ── collected values ─────────────────────────────────────────────────

def values(samples: List[Sample], sensor: str, name: str) -> List[float]:
    out = []
    for _, line in samples:
        sec = line.get(sensor)
        if not isinstance(sec, dict):
            continue
        if name == "dew":
            t, h = sec.get("temp"), sec.get("hum")
            dp = dew_point(t, h) if isinstance(t, (int, float)) and isinstance(h, (int, float)) else None
            if dp is not None:
                out.append(dp)
        elif isinstance(sec.get(name), (int, float)) and not isinstance(sec.get(name), bool):
            out.append(float(sec[name]))
    return out


def med(samples, sensor, name) -> Optional[float]:
    v = values(samples, sensor, name)
    return statistics.median(v) if v else None


def fmt(v, dp=1):
    return "—" if v is None else f"{v:.{dp}f}"


def table(samples: List[Sample]) -> str:
    rows, last = [], None
    for (sensor, name), (label, unit, dp) in FIELDS.items():
        v = values(samples, sensor, name)
        if not v:
            continue
        if sensor != last:
            rows.append(f"\n  {SENSORS[sensor]}  ({len(v)} readings)")
            last = sensor
        if unit == "":
            rows.append(f"    {label:<28} {'yes' if v[-1] else 'no'}")
        else:
            rows.append(f"    {label:<28} {v[-1]:>9.{dp}f} {unit:<5}  min {min(v):.{dp}f}  max {max(v):.{dp}f}")
    return "\n".join(rows)


# ── automatic checks ─────────────────────────────────────────────────

@dataclass
class Check:
    sensor: str
    name: str
    status: str          # OK | WARN | FAIL | INFO
    detail: str
    hint: str = ""


def band(v: float, ok: Tuple[float, float], warn: Tuple[float, float] = None) -> str:
    if ok[0] <= v <= ok[1]:
        return "OK"
    if warn and warn[0] <= v <= warn[1]:
        return "WARN"
    return "FAIL"


def expected_o2(co2_ppm: float) -> float:
    """O₂ % expected from CO₂: breathing turns O₂ into CO₂ about 1.2 : 1 (respiratory quotient 0.85)."""
    return 20.95 - 1.2 * max(0.0, co2_ppm - 420) / 1e4


def auto_checks(samples: List[Sample], notes: List[str]) -> List[Check]:
    c: List[Check] = []
    lines = max(1, len(samples))
    seen = {s: sum(1 for _, ln in samples if isinstance(ln.get(s), dict)) for s in SENSORS}

    # present at all
    for s, n in seen.items():
        need = lines / 10 if s == "scd40" else lines / 2          # the SCD40 measures every 5 s
        if n == 0:
            hint = {"mq4": "no MQ-4 signal on GPIO32 (under 40 mV or over 6.2 V at AO): check 5 V to the module, "
                           "AO → divider → GPIO32 and GND",
                    "scd40": "not on the I2C bus at 0x62, or no measurement yet (first one after 5 s): check 3.3 V/SDA/SCL"
                    }.get(s, "not found on the I2C bus: check its 3.3 V, GND, SDA (GPIO21) and SCL (GPIO22)")
            c.append(Check(s, "present", "FAIL", "no readings", hint))
        elif n < need:
            c.append(Check(s, "present", "WARN", f"only {n} readings in {lines} lines", "loose wire or bus errors"))

    t, h, p = med(samples, "bme280", "temp"), med(samples, "bme280", "hum"), med(samples, "bme280", "pres")
    if t is not None:
        c.append(Check("bme280", "temperature", band(t, (5, 45)), f"{t:.1f} °C"))
    if h is not None:
        c.append(Check("bme280", "humidity", band(h, (5, 95), (1, 99)), f"{h:.1f} %RH",
                       "0 or 100 % means a damaged humidity element"))
    if p is not None:
        c.append(Check("bme280", "pressure", band(p, (950, 1050), (850, 1085)), f"{p:.1f} hPa",
                       "near sea level (Mumbai) the air pressure is 995-1020 hPa"))
    if t is not None:
        if len(set(values(samples, "bme280", "temp"))) == 1 and len(values(samples, "bme280", "temp")) >= 20:
            c.append(Check("bme280", "changing", "WARN", "temperature exactly the same in every reading",
                           "a live sensor shows ±0.01 °C noise: re-flash, or check STATUS"))

    co2, st, sh = med(samples, "scd40", "co2_ppm"), med(samples, "scd40", "temp"), med(samples, "scd40", "hum")
    if co2 is not None:
        c.append(Check("scd40", "CO₂", band(co2, (380, 1000), (1000, 5000)) if co2 >= 380 else "FAIL", f"{co2:.0f} ppm",
                       "outdoor air is ~420 ppm, so less is impossible: the SCD40's self-calibration corrects it within "
                       "about a week in a room that gets fresh air" if co2 < 380 else
                       "above 1000 ppm the room needs fresh air (the sensor is fine)" if co2 > 1000 else ""))
    if st is not None:
        c.append(Check("scd40", "temperature", band(st, (5, 50)), f"{st:.1f} °C"))
    if sh is not None:
        c.append(Check("scd40", "humidity", band(sh, (5, 95), (1, 99)), f"{sh:.1f} %RH"))
    if t is not None and st is not None:
        d = st - t
        c.append(Check("scd40", "temperature vs BME280", band(abs(d), (0, 2), (0, 5)), f"{d:+.1f} °C",
                       "the SCD40 warms itself and sits near the ESP32 and buck converter: a few °C more is normal; "
                       "more than 5 °C means one of them is wrong"))
        dpb, dps = med(samples, "bme280", "dew"), med(samples, "scd40", "dew")
        if dpb is not None and dps is not None:
            c.append(Check("scd40", "dew point vs BME280", band(abs(dps - dpb), (0, 1.5), (0, 3)),
                           f"{dps:.1f} vs {dpb:.1f} °C",
                           "the same air has the same dew point: a bigger gap means one humidity sensor reads wrong"))

    o2 = med(samples, "o2", "o2_pct")
    if o2 is not None:
        exp = expected_o2(co2) if co2 is not None else 20.95
        st_o2 = band(o2, (20.4, 21.4), (19.5, 22.0))
        why = f"{o2:.2f} %"
        if co2 is not None:
            why += f"; CO₂ {co2:.0f} ppm means the air has about {exp:.1f} % O₂"
        c.append(Check("o2", "O₂", st_o2, why,
                       "" if st_o2 == "OK" else
                       "the sensor needs calibrating, not the air: take the board outdoors (or to an open window) "
                       "for 5 min and send CAL_O2 (esp32_bridge.py --send CAL_O2)" if abs(o2 - exp) > 0.6 else
                       "O₂ and CO₂ agree: the room air itself is off, ventilate"))

    notes_text = " ".join(notes)
    if "self-test" in notes_text:
        failed = [x for x in ("accel", "mag", "gyro", "mcu") if f"{x}=FAIL" in notes_text]
        c.append(Check("bno055", "self-test", "FAIL" if failed else "OK",
                       "failed: " + ", ".join(failed) if failed else "accelerometer, magnetometer, gyro and MCU passed",
                       "a part of the chip failed its power-on test: power-cycle; if it stays, the BNO055 is damaged"
                       if failed else ""))
    g, mag, gyro, la = (med(samples, "bno055", k) for k in ("grav_ms2", "mag_ut", "gyro_dps", "lin_acc_ms2"))
    if g is not None:
        c.append(Check("bno055", "gravity", band(g, (9.5, 10.1), (9.0, 10.6)), f"{g:.2f} m/s² (Earth: 9.81)",
                       "accelerometer off: rest the board in 6 positions (each side up) for a few seconds each"))
    if mag is not None:
        c.append(Check("bno055", "magnetic field", band(mag, (25, 65), (10, 100)), f"{mag:.1f} µT (Mumbai: ~42 µT)",
                       "iron or a magnet nearby (speaker, laptop, steel desk, the buck converter's coil): heading "
                       "will be off; move it away, then wave the board in a figure-8"))
    if gyro is not None and la is not None and gyro < 1.5:
        c.append(Check("bno055", "at rest", "OK" if la < 0.3 else "WARN", f"motion {la:.2f} m/s², rotation {gyro:.2f} °/s",
                       "shows motion while lying still: accelerometer not yet calibrated"))
    cal = [med(samples, "bno055", k) for k in ("imu_calib", "calib_gyro", "calib_acc", "calib_mag")]
    if cal[0] is not None:
        parts = "system {:.0f}, gyro {:.0f}, accelerometer {:.0f}, magnetometer {:.0f} (of 3)".format(
            *(v if v is not None else 0 for v in cal))
        c.append(Check("bno055", "calibration", "OK" if cal[0] == 3 else "WARN", parts,
                       "gyro: keep still 3 s; accelerometer: rest on each of 6 sides; magnetometer: slow figure-8. "
                       "The BNO055 forgets this at power-off"))
    r, pi = med(samples, "bno055", "roll_deg"), med(samples, "bno055", "pitch_deg")
    if r is not None and pi is not None:
        c.append(Check("bno055", "orientation", "INFO", f"tilted {math.hypot(r, pi):.0f}° from level "
                       f"(roll {r:.1f}°, pitch {pi:.1f}°)", "flat on a level table should read within ~3°"))

    vo = values(samples, "mq4", "vout_mv")
    if vo:
        m = statistics.median(vo)
        c.append(Check("mq4", "signal", band(m, (100, 4700), (50, 4950)), f"{m:.0f} mV at AO (0-5000)",
                       "near 0: module unpowered or AO not connected; near 5 V: saturated (gas present?)"))
        if len(vo) >= 10 and m > 0:
            spread = statistics.pstdev(vo) / m
            c.append(Check("mq4", "steady", "OK" if spread < 0.05 else "WARN", f"±{spread * 100:.1f} % noise",
                           "a noisy output: check the module's GND to the ESP32 and the 5 V supply"))
        if values(samples, "mq4", "warming")[-1:] == [1.0]:
            c.append(Check("mq4", "warm-up", "INFO", "heating (first 3 min after power-on): no ppm yet"))
        if values(samples, "mq4", "calibrated")[-1:] == [0.0]:
            c.append(Check("mq4", "calibration", "WARN", "not calibrated: only mV and Rs/RL, no ppm",
                           "after 24-48 h powered on, in clean air: esp32_bridge.py --send CAL_MQ4"))
        rr = med(samples, "mq4", "rs_r0")
        if rr is not None:
            c.append(Check("mq4", "clean air", band(rr, (3.0, 6.5), (2.0, 8.0)), f"Rs/R0 {rr:.2f} (clean air: ~4.4)",
                           "below 3: gas is present now, or CAL_MQ4 was done in air that wasn't clean" if rr < 3 else
                           "above 6.5: CAL_MQ4 was done with gas around or before the heater was warm; redo it in clean air"))
        ppm = med(samples, "mq4", "ch4_ppm")
        if ppm is not None:
            c.append(Check("mq4", "CH₄", "INFO", f"{ppm:.0f} ppm",
                           "the MQ-4 measures 200-10000 ppm; below 200 ppm read it as 'no methane' (air has ~2 ppm)"))
    return c


# ── hands-on tests ───────────────────────────────────────────────────

@dataclass
class Handson:
    key: str
    title: str
    instructions: str
    watch: List[Tuple[str, str]]                        # fields shown live while it runs
    judge: Callable[[List[Sample], List[Sample]], List[Check]]   # (before, after) → checks
    seconds: int = 40


def _rise(before, after, sensor, name) -> Optional[float]:
    b, a = values(before, sensor, name), values(after, sensor, name)
    return (max(a) - statistics.median(b)) if a and b else None


def _fall(before, after, sensor, name) -> Optional[float]:
    b, a = values(before, sensor, name), values(after, sensor, name)
    return (statistics.median(b) - min(a)) if a and b else None


def _angle_change(before, after, name) -> Optional[float]:
    b, a = values(before, "bno055", name), values(after, "bno055", name)
    if not a or not b:
        return None
    base = statistics.median(b)
    return max(abs((x - base + 180) % 360 - 180) for x in a)


def judge_breath(before, after):
    out = []
    d = _rise(before, after, "scd40", "co2_ppm")
    out.append(Check("scd40", "breath: CO₂ rises", "FAIL" if d is None or d < 150 else "OK",
                     "no reading" if d is None else f"+{d:.0f} ppm", "breath is ~40000 ppm CO₂: a real SCD40 jumps "
                     "hundreds of ppm within 10-20 s"))
    for s in ("bme280", "scd40"):
        d = _rise(before, after, s, "hum")
        out.append(Check(s, "breath: humidity rises", "FAIL" if d is None or d < 2 else "OK",
                         "no reading" if d is None else f"+{d:.1f} %RH"))
    d = _fall(before, after, "o2", "o2_pct")
    if d is not None:
        out.append(Check("o2", "breath: O₂ dips", "OK" if d >= 0.1 else "INFO", f"-{d:.2f} %",
                         "breath is ~16 % O₂ but mixes with room air: a small dip, and a slow electrochemical cell "
                         "may not show it; CAL_O2 in fresh air is the real O₂ check"))
    return out


def judge_warm(before, after):
    d = _rise(before, after, "bme280", "temp")
    return [Check("bme280", "hand warmth: temperature rises", "FAIL" if d is None or d < 0.4 else "OK",
                  "no reading" if d is None else f"+{d:.2f} °C")]


def judge_tilt(before, after):
    d = max((x for x in (_angle_change(before, after, "roll_deg"), _angle_change(before, after, "pitch_deg"))
             if x is not None), default=None)
    out = [Check("bno055", "tilt: roll/pitch follow", "FAIL" if d is None or d < 45 else "OK",
                 "no reading" if d is None else f"{d:.0f}° change")]
    g = [v for v in values(after, "bno055", "grav_ms2")]
    if g:
        out.append(Check("bno055", "tilt: gravity stays 9.8", band(statistics.median(g), (9.5, 10.1), (9.0, 10.6)),
                         f"{statistics.median(g):.2f} m/s²"))
    gy = values(after, "bno055", "gyro_dps")
    if gy:
        out.append(Check("bno055", "tilt: gyro sees the turn", "OK" if max(gy) >= 20 else "FAIL", f"peak {max(gy):.0f} °/s"))
    return out


def judge_turn(before, after):
    d = _angle_change(before, after, "heading_deg")
    cal = values(after, "bno055", "calib_mag")
    return [Check("bno055", "turn: heading follows", "FAIL" if d is None or d < 45 else "OK",
                  "no reading" if d is None else f"{d:.0f}° change",
                  "" if cal and cal[-1] >= 2 else "magnetometer not calibrated yet: wave the board in a figure-8 first")]


def judge_gas(before, after):
    d = _rise(before, after, "mq4", "vout_mv")
    return [Check("mq4", "gas: output rises", "FAIL" if d is None or d < 150 else "OK",
                  "no reading" if d is None else f"+{d:.0f} mV",
                  "the heater must be warm (3 min after power-on); a cold MQ-4 barely responds")]


HANDSON = [
    Handson("breath", "Breath: SCD40 CO₂, both humidity sensors, O₂",
            "From about 10 cm, breathe slowly onto the board: 3 long breaths over 10 s. Then wait.",
            [("scd40", "co2_ppm"), ("scd40", "hum"), ("bme280", "hum"), ("o2", "o2_pct")], judge_breath, 45),
    Handson("warm", "Hand warmth: BME280 temperature",
            "Cup your hand around the BME280 (small silver chip), without touching the pins, for 20 s.",
            [("bme280", "temp")], judge_warm, 30),
    Handson("tilt", "Tilt: BNO055 roll/pitch, gravity, gyro",
            "Tilt the board up onto one edge (about 90°), hold 3 s, lay it flat again.",
            [("bno055", "roll_deg"), ("bno055", "pitch_deg"), ("bno055", "grav_ms2"), ("bno055", "gyro_dps")],
            judge_tilt, 20),
    Handson("turn", "Turn: BNO055 heading (compass)",
            "Keep the board flat and turn it on the table by about 90°, like a compass. Hold.",
            [("bno055", "heading_deg"), ("bno055", "calib_mag")], judge_turn, 20),
    Handson("gas", "Gas: MQ-4 (optional)",
            "Hold an UNLIT gas lighter 2-3 cm from the MQ-4 and press the gas button for 2 s: no flame, no\n"
            "    sparks, away from anything hot. Or hold a tissue with hand sanitizer near it. Air the room after.",
            [("mq4", "vout_mv"), ("mq4", "rs_rl")], judge_gas, 40),
]
PASS_EARLY = {"breath": lambda b, a: all(x.status == "OK" for x in judge_breath(b, a) if x.sensor != "o2"),
              "warm": lambda b, a: judge_warm(b, a)[0].status == "OK",
              "tilt": lambda b, a: judge_tilt(b, a)[0].status == "OK" and judge_tilt(b, a)[-1].status == "OK",
              "turn": lambda b, a: judge_turn(b, a)[0].status == "OK",
              "gas": lambda b, a: judge_gas(b, a)[0].status == "OK"}


# ── talking to the board ─────────────────────────────────────────────

class Reader(threading.Thread):
    """Collects the board's lines in the background."""

    def __init__(self, ser):
        super().__init__(daemon=True)
        self.ser, self.samples, self.notes, self.stop = ser, [], [], False

    def run(self):
        while not self.stop:
            try:
                parsed = parse_line(self.ser.readline().decode("utf-8", "replace"))
            except Exception as e:
                self.notes.append(f"serial error: {e}")
                time.sleep(1)
                continue
            if parsed and parsed[0] == "data":
                self.samples.append((time.time(), parsed[1]))
            elif parsed and parsed[0] == "info":
                self.notes.append(parsed[1])

    def since(self, t0) -> List[Sample]:
        return [s for s in list(self.samples) if s[0] >= t0]


def live_line(samples: List[Sample], fields) -> str:
    if not samples:
        return "(waiting for the board)"
    parts = []
    for sensor, name in fields:
        v = values(samples[-6:], sensor, name)
        label, unit, dp = FIELDS[(sensor, name)]
        parts.append(f"{sensor} {label.split(' (')[0].lower()} {fmt(v[-1] if v else None, dp)}{unit}")
    return " | ".join(parts)


def show(checks: List[Check]):
    mark = {"OK": "✓", "WARN": "!", "FAIL": "✗", "INFO": "·"}
    for ch in checks:
        print(f"  {mark[ch.status]} {ch.status:<4} {ch.sensor:<7} {ch.name}: {ch.detail}")
        if ch.hint and ch.status in ("WARN", "FAIL"):
            print(f"         → {ch.hint}")


def service_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], capture_output=True).returncode == 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="no hands-on tests")
    parser.add_argument("--seconds", type=int, default=30, help="how long to collect before checking (default 30)")
    parser.add_argument("--only", help="comma-separated hands-on tests: " + ",".join(h.key for h in HANDSON))
    args = parser.parse_args(argv)

    port = esp32_port()
    if not port:
        print("✗ no ESP32 on USB: plug the board into a Pi USB port (ls /dev/ttyUSB*)")
        return 2
    restart = False
    try:
        restart = service_active()
    except FileNotFoundError:
        pass
    if restart:
        if os.geteuid() != 0:
            print("✗ the esp32_bridge service holds the port: run with sudo")
            return 2
        print(f"· stopping {SERVICE} while checking (started again at the end)")
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        time.sleep(1)
    try:
        return run(port, args)
    finally:
        if restart:
            subprocess.run(["systemctl", "start", SERVICE], check=False)
            print(f"· {SERVICE} started again: data flows to IMM-OS")


def run(port, args) -> int:
    ser = open_port(port)
    reader = Reader(ser)
    reader.start()
    ser.write(b"STATUS\n")
    print(f"ESP32 on {port}: collecting {args.seconds} s…")
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(1)
        overview = [("bme280", "temp"), ("scd40", "co2_ppm"), ("o2", "o2_pct"), ("mq4", "vout_mv"), ("bno055", "heading_deg")]
        print("  " + live_line(reader.samples, overview), flush=True)
    samples = reader.since(t0)
    board_notes = [n for n in reader.notes if n.startswith(("sensors:", "bno055 self-test"))]
    for n in board_notes:
        print("  board: " + n)
    print("\n── Every value (latest, and min/max over the window) ──" + table(samples))
    print("\n── Automatic checks ──")
    checks = auto_checks(samples, reader.notes)
    show(checks)

    if not args.auto:
        chosen = [h for h in HANDSON if not args.only or h.key in args.only.split(",")]
        print("\n── Hands-on tests ── (Enter = start, s = skip, q = stop testing)")
        for h in chosen:
            print(f"\n{h.title}\n    {h.instructions}")
            answer = input("    ready? ").strip().lower()
            if answer == "q":
                break
            if answer == "s":
                continue
            start = time.time()
            before = [s for s in reader.samples if start - 15 <= s[0] < start]
            while time.time() - start < h.seconds:
                time.sleep(1)
                after = reader.since(start)
                print(f"    {time.time() - start:4.0f} s  " + live_line(after, h.watch), flush=True)
                if PASS_EARLY[h.key](before, after):
                    break
            result = h.judge(before, reader.since(start))
            show(result)
            checks += result
    reader.stop = True

    print("\n── Summary ──")
    for s, title in SENSORS.items():
        mine = [ch for ch in checks if ch.sensor == s]
        worst = "FAIL" if any(ch.status == "FAIL" for ch in mine) else "WARN" if any(ch.status == "WARN" for ch in mine) \
            else "OK" if mine else "—"
        print(f"  {worst:<4} {title}")
    fails = sum(ch.status == "FAIL" for ch in checks)
    print(json.dumps({"ok": sum(ch.status == "OK" for ch in checks), "warn": sum(ch.status == "WARN" for ch in checks),
                      "fail": fails}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
