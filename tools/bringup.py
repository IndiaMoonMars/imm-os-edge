#!/usr/bin/env python3
"""
Sensor bring-up: check one sensor at a time on the bench before it goes live.

  bringup.py                  board + bus report (model, I2C devices, UART, SPI, 1-Wire, power)
  bringup.py bme280           that report for the sensor's bus, then run its real driver
                              (stdout only, nothing is published) and check every value
  bringup.py bme280 -n 5      wait for 5 readings (default 3)
  bringup.py --list           the sensors it knows

Run it as the service user with the node's settings, e.g.
  sudo -u ubuntu env $(sudo grep -vE '^(#|$|MQTT_PASSWORD|IMM_EDGE_CLIENT_SECRET)' /etc/imm-os/edge.env | xargs) \\
       .venv/bin/python tools/bringup.py scd40
or simply `sudo .venv/bin/python tools/bringup.py scd40`, which reads /etc/imm-os/edge.env itself
(secrets are never passed to the driver).

Exit status: 0 all checks passed, 1 something failed, 2 usage error.
Once a sensor passes: add it to IMM_SENSORS (setup-node.sh --sensors) and switch it off
in the MCC simulator (SIM_DISABLED_SENSORS=<node>:<sensor>).
"""
import argparse
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from hw import board_model, default_uart, i2c_bus_number, is_pi5  # noqa: E402

ENV_FILE = os.getenv("IMM_ENV_FILE", "/etc/imm-os/edge.env")
SECRET_KEYS = {"MQTT_PASSWORD", "IMM_EDGE_CLIENT_SECRET"}

I2C_NAMES = {0x36: "MAX17048 fuel gauge", 0x39: "TSL2561", 0x40: "INA219/PCA9685", 0x41: "INA219 (A0)",
             0x44: "INA219 (A1)", 0x48: "ADS1115", 0x49: "ADS1115 (ADDR→VDD)", 0x57: "MAX30100",
             0x5A: "MLX90614", 0x62: "SCD40/41", 0x63: "EZO-pH", 0x70: "TCA9548A mux",
             0x76: "BME280", 0x77: "BME280 (SDO high)"}


def _addr(env: str, default: int) -> Optional[int]:
    raw = os.getenv(env, hex(default)).strip().lower()
    return None if raw in ("", "none", "off") else int(raw, 0)


@dataclass
class Sensor:
    driver: str                               # path under the repo
    ranges: Dict[str, Tuple[float, float]]    # metric → plausible (lo, hi)
    i2c: List[int] = field(default_factory=list)
    uart: bool = False
    timeout_s: float = 20.0
    args: List[str] = field(default_factory=list)
    tip: str = ""                             # sanity check against a reference
    simulated: bool = False                   # the MCC simulator fakes this sensor today
    count: Optional[int] = None               # readings to wait for (default: --count)
    chip_id: Optional[Tuple[int, int, Dict[int, str]]] = None   # (address, register, {value: name}) to identify the part


def sensors() -> Dict[str, Sensor]:
    return {
        "bme280": Sensor("sensor_drivers/bme280_driver.py", {"temp": (-10, 60), "hum": (0, 100), "pres": (300, 1100)},
                         i2c=[_addr("BME280_ADDRESS", 0x76)], simulated=True,
                         chip_id=(_addr("BME280_ADDRESS", 0x76), 0xD0, {0x60: "BME280", 0x58: "BMP280 (no humidity!)"}),
                         tip="compare temperature with a room thermometer (±1 °C), pressure with a weather site (±2 hPa)"),
        "scd40": Sensor("sensor_drivers/scd40_driver.py", {"co2_ppm": (250, 5000), "temp": (-10, 60), "hum": (0, 100)},
                        i2c=[0x62], timeout_s=30, simulated=True,
                        tip="fresh air reads ~420 ppm; breathe near it and CO₂ should jump within 10 s"),
        "o2": Sensor("sensor_drivers/o2_driver.py", {"o2_pct": (19.0, 23.0)}, i2c=[_addr("O2_ADS_ADDRESS", 0x49)],
                     simulated=True,
                     tip="needs O2_CAL_MV (run the driver with --calibrate in fresh air first); air is 20.9 %"),
        "ina219": Sensor("sensor_drivers/power_driver.py", {"voltage_v": (0, 26), "current_ma": (-3200, 3200)},
                         i2c=[_addr("INA219_ADDRESS", 0x40)], tip="compare bus voltage with a multimeter (±0.05 V)"),
        "tsl2561": Sensor("sensor_drivers/lux_driver.py", {"lux": (0, 40000)}, i2c=[0x70],
                          tip="cover a sensor: lux → ~0; phone torch: lux jumps into the thousands"),
        "ecg": Sensor("sensor_drivers/ecg_driver.py", {"voltage": (0, 3.3)}, i2c=[_addr("ECG_ADS_ADDRESS", 0x48)],
                      tip="electrodes on: the trace should show beats; LO+/LO- high means a lead is off"),
        "max30100": Sensor("sensor_drivers/biosensor_driver.py", {"hr_bpm": (35, 200), "spo2_pct": (85, 100)},
                           i2c=[0x57], timeout_s=40,
                           tip="nothing is published until a finger rests on it; hold still ~10 s; compare with a pulse oximeter"),
        "mq7": Sensor("sensor_drivers/mq7_uart_bridge.py", {"co_ppm": (0, 1000)}, uart=True, timeout_s=320, count=1,
                      tip="the STM32 reports once per 150 s heater cycle; calibrate in clean air with "
                          "mq7_uart_bridge.py --calibrate; readings mean little before 24–48 h burn-in"),
        "sysmon": Sensor("sensor_drivers/sysmon_driver.py", {"cpu_temp": (0, 85), "undervolt": (0, 0), "throttled": (0, 0)},
                         args=["--interval", "2"], timeout_s=15, simulated=True,
                         tip="undervolt=1 means the power supply is too weak (Pi 5: use the 27 W 5 V/5 A supply)"),
        "bms": Sensor("sensor_drivers/bms_driver.py", {"battery_pct": (0, 100), "solar_w": (0, 500)},
                      i2c=[a for a in (_addr("BMS_GAUGE_ADDRESS", 0x36),) if a is not None],
                      args=["--interval", "2"], timeout_s=15, simulated=True, tip="compare battery % with the UPS board's LEDs"),
    }


# ── environment ────────────────────────────────────────────────────

def load_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE pairs from edge.env, without the secrets (the drivers don't need them here)."""
    from envfile import _KEY, unquote
    out = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = _KEY.match(line)
        if m and m.group(1) not in SECRET_KEYS:
            out[m.group(1)] = unquote(line.split("=", 1)[1])
    return out


# ── bus checks ─────────────────────────────────────────────────────

def i2c_scan(bus, addresses=range(0x03, 0x78)) -> List[int]:
    """Addresses that ACK, probed the way i2cdetect does (read for EEPROM-ish ranges, else quick write)."""
    found = []
    for a in addresses:
        try:
            if 0x30 <= a <= 0x37 or 0x50 <= a <= 0x5F:
                bus.read_byte(a)
            else:
                bus.write_quick(a)
            found.append(a)
        except OSError:
            continue
    return found


def open_i2c():
    from smbus2 import SMBus
    return SMBus(i2c_bus_number())


def serial_console_on(port: str, cmdline: str) -> bool:
    names = {os.path.basename(port), "serial0"}
    return any(f"console={n}" in cmdline for n in names)


def throttled_flags() -> Optional[int]:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


class Report:
    def __init__(self):
        self.fails = 0

    def ok(self, msg):
        print(f"  ✓ {msg}")

    def warn(self, msg):
        print(f"  ! {msg}")

    def fail(self, msg):
        print(f"  ✗ {msg}")
        self.fails += 1


def board_report(r: Report, want_i2c: List[int] = None, want_uart: bool = False, full: bool = True) -> None:
    model = board_model() or "unknown board"
    print(f"── Board: {model}{' (Pi 5 / RP1)' if is_pi5(model) else ''}")
    bits = throttled_flags()
    if bits is not None:
        if bits & 0x1:
            r.fail("under-voltage NOW: power supply too weak (Pi 5 needs the 27 W 5 V/5 A supply)")
        elif bits & 0x10000:
            r.warn(f"under-voltage happened since boot (throttled=0x{bits:x}); check the supply and cable")
        else:
            r.ok(f"power OK (throttled=0x{bits:x})")

    if full or want_i2c:
        dev = f"/dev/i2c-{i2c_bus_number()}"
        if not os.path.exists(dev):
            r.fail(f"{dev} missing: enable I2C (raspi-config nonint do_i2c 0) and reboot")
        else:
            try:
                found = i2c_scan(open_i2c())
            except OSError as e:
                r.fail(f"cannot open {dev}: {e} (is the user in the i2c group?)")
                found = None
            if found is not None:
                names = ", ".join(f"0x{a:02x} {I2C_NAMES.get(a, '?')}" for a in found) or "nothing"
                print(f"  · I2C bus {i2c_bus_number()}: {names}")
                for a in want_i2c or []:
                    if a in found:
                        r.ok(f"0x{a:02x} answers")
                    else:
                        r.fail(f"0x{a:02x} ({I2C_NAMES.get(a, 'expected device')}) not found: check 3.3 V, GND, "
                               "SDA→pin 3, SCL→pin 5, and the board's address jumper")

    if full or want_uart:
        port = os.getenv("MQ7_PORT") or os.getenv("GPS_PORT") or default_uart()
        if os.path.exists(port):
            r.ok(f"UART {port}" + (f" → {os.path.realpath(port)}" if os.path.islink(port) else ""))
            try:
                with open("/proc/cmdline") as f:
                    if serial_console_on(port, f.read()):
                        r.fail("the login console is on this UART: raspi-config nonint do_serial_cons 1, then reboot")
            except OSError:
                pass
        else:
            hint = "add dtparam=uart0=on to /boot/firmware/config.txt" if is_pi5(model) else "raspi-config nonint do_serial_hw 0"
            (r.fail if want_uart else r.warn)(f"UART {port} missing: {hint}, then reboot")

    if full:
        spi = glob.glob("/dev/spidev0.*")
        (r.ok if spi else r.warn)(f"SPI: {', '.join(spi)}" if spi else "SPI off (only needed for the RC522 RFID reader)")
        w1 = glob.glob("/sys/bus/w1/devices/28-*")
        if w1:
            r.ok(f"1-Wire DS18B20: {', '.join(os.path.basename(p) for p in w1)}")
        else:
            print("  · 1-Wire: no DS18B20 found (only needed for the biolab probe)")


# ── driver run ─────────────────────────────────────────────────────

def identify(bus, address: int, register: int, names: Dict[int, str]) -> Tuple[Optional[int], str]:
    try:
        value = bus.read_byte_data(address, register)
    except OSError as e:
        return None, f"cannot read ID register 0x{register:02x}: {e}"
    return value, names.get(value, f"unknown part (ID 0x{value:02x})")


def run_driver(cmd: List[str], env: dict, count: int, timeout_s: float, notes: List[str] = None) -> Tuple[List[dict], List[str]]:
    """
    Run a driver in stdout mode until `count` JSON readings arrive or the timeout.
    Returns (readings, errors); stderr lines that are {"info": ...} go to `notes` instead.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
    readings, errors = [], []
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)                       # driver exited

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout_s
    try:
        while len(readings) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                readings.append(msg)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        err = proc.stderr.read() or ""
    for line in err.splitlines():
        try:
            msg = json.loads(line)
            if isinstance(msg, dict) and "info" in msg and "error" not in msg:
                if notes is not None:
                    notes.append(str(msg["info"]))
                continue
            errors.append(msg.get("error", line) if isinstance(msg, dict) else line)
        except ValueError:
            if line.strip():
                errors.append(line.strip())
    return readings[:count], errors


def check_values(readings: List[dict], ranges: Dict[str, Tuple[float, float]]) -> List[Tuple[str, str, str]]:
    """[(metric, 'ok'|'bad'|'missing', detail)] over all readings."""
    results = []
    for metric, (lo, hi) in ranges.items():
        vals = [r[metric] for r in readings if isinstance(r.get(metric), (int, float))]
        if not vals:
            results.append((metric, "missing", "not in any reading"))
            continue
        bad = [v for v in vals if not lo <= v <= hi]
        span = f"{min(vals):g}" if min(vals) == max(vals) else f"{min(vals):g} … {max(vals):g}"
        results.append((metric, "bad" if bad else "ok", f"{span} (expected {lo:g} … {hi:g})"))
    return results


def bringup(name: str, sensor: Sensor, count: int, python: str) -> int:
    r = Report()
    board_report(r, want_i2c=sensor.i2c, want_uart=sensor.uart, full=False)
    if r.fails:
        print(f"\n{name}: fix the bus problem above first.")
        return 1

    if sensor.chip_id:
        value, part = identify(open_i2c(), *sensor.chip_id)
        if value is None or value not in sensor.chip_id[2] or "!" in part:
            r.fail(f"chip: {part}")
        else:
            r.ok(f"chip: {part}")
    ads = {k: _addr(k, d) for k, d in (("ECG_ADS_ADDRESS", 0x48), ("O2_ADS_ADDRESS", 0x49))}
    if name in ("ecg", "o2") and ads["ECG_ADS_ADDRESS"] == ads["O2_ADS_ADDRESS"]:
        r.warn("ECG and O2 share one ADS1115: don't run both drivers on this node (use a second ADS1115 at 0x49)")

    count = sensor.count or count
    wait = f" (up to {sensor.timeout_s:g} s)" if sensor.timeout_s > 60 else ""
    print(f"── Running {sensor.driver} (stdout only, nothing is published) for {count} reading(s){wait}…")
    env = dict(os.environ)
    notes: List[str] = []
    readings, errors = run_driver([python, sensor.driver, "--mode", "stdout", *sensor.args], env, count,
                                  sensor.timeout_s, notes)
    for n in notes[-3:]:
        print(f"  · {n}")
    for e in errors[:5]:
        r.fail(f"driver: {e}")
    if not readings:
        r.fail(f"no readings within {sensor.timeout_s:g} s")
    else:
        last = readings[-1]
        values = {k: v for k, v in last.items() if k not in ("sensor", "timestamp", "node_id", "zone", "simulated")}
        print(f"  · node {last.get('node_id')} zone {last.get('zone')}: {json.dumps(values)}")
        if len(readings) < count:
            r.warn(f"only {len(readings)} of {count} readings within {sensor.timeout_s:g} s")
        for metric, state, detail in check_values(readings, sensor.ranges):
            {"ok": r.ok, "bad": r.fail, "missing": r.warn}[state](f"{metric}: {detail}")
    if sensor.tip:
        print(f"  → {sensor.tip}")
    print()
    if r.fails:
        print(f"{name}: {r.fails} problem(s).")
        return 1
    driver = os.path.basename(sensor.driver)
    print(f"{name}: PASS. Next: add {driver} to this node's sensors "
          f"(sudo scripts/setup-node.sh --sensors \"… {driver}\")")
    if sensor.simulated:
        print(f"  and stop the MCC simulator faking it: SIM_DISABLED_SENSORS=<node>:{name} in imm-os-infra/.env, "
              "then docker compose up -d sensor-sim")
    return 0


def main(argv=None) -> int:
    known = sensors()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sensor", nargs="?", choices=sorted(known), metavar="SENSOR")
    p.add_argument("-n", "--count", type=int, default=3)
    p.add_argument("--list", action="store_true")
    p.add_argument("--python", default=sys.executable, help="interpreter for the driver (default: this one)")
    args = p.parse_args(argv)

    if args.list:
        for name, s in sorted(known.items()):
            bus = ", ".join(f"I2C 0x{a:02x}" for a in s.i2c) or ("UART" if s.uart else "on-board")
            print(f"  {name:9} {bus:18} {s.driver}")
        return 0

    for k, v in load_env_file(ENV_FILE).items():
        os.environ.setdefault(k, v)
    known = sensors()     # re-read addresses now that edge.env is loaded

    if not args.sensor:
        r = Report()
        board_report(r)
        print(f"\n{'All board checks passed.' if not r.fails else f'{r.fails} problem(s).'}  "
              "Next: bringup.py <sensor> for each sensor you have wired (see --list).")
        return 1 if r.fails else 0
    return bringup(args.sensor, known[args.sensor], args.count, args.python)


if __name__ == "__main__":
    sys.exit(main())
