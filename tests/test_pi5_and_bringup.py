"""Raspberry Pi 5 support, node health / BMS drivers, RC522 driver and the bring-up tool (no hardware needed)."""
import os
import pathlib
import re
import sys

import pytest

import bringup
import hw
import rc522
import sysmon_driver
from bms_driver import FuelGauge, read_once as bms_read_once, soc_from_bytes

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── board detection ────────────────────────────────────────────────

def test_board_model_strips_nul(tmp_path):
    f = tmp_path / "model"
    f.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\0")
    assert hw.board_model(str(f)) == "Raspberry Pi 5 Model B Rev 1.0"
    assert hw.board_model(str(tmp_path / "missing")) == ""


@pytest.mark.parametrize("model,has_ama0,port", [
    ("Raspberry Pi 5 Model B Rev 1.0", True, "/dev/ttyAMA0"),
    ("Raspberry Pi 5 Model B Rev 1.0", False, "/dev/serial0"),    # UART0 not enabled yet
    ("Raspberry Pi 4 Model B Rev 1.5", True, "/dev/serial0"),
    ("", False, "/dev/serial0"),
])
def test_default_uart(model, has_ama0, port):
    assert hw.default_uart(model, exists=lambda p: has_ama0 and p == "/dev/ttyAMA0") == port


def test_uart_port_env_wins(monkeypatch):
    monkeypatch.setenv("GPS_PORT", "/dev/ttyUSB0")
    assert hw.uart_port("GPS_PORT") == "/dev/ttyUSB0"


def test_no_driver_imports_board():
    """`import board` needs libgpiod bindings on a Pi 5; drivers must use hw.i2c_bus()."""
    offenders = [str(p.relative_to(ROOT)) for d in ("sensor_drivers", "eclss", "eva", "core", "inventory")
                 for p in (ROOT / d).glob("*.py") if re.search(r"^\s*import board\b", p.read_text(), re.M)]
    assert offenders == []


def test_no_rpi_gpio_dependency():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    assert "rpi.gpio" not in reqs and "mfrc522" not in reqs


# ── RC522 (emulated chip) ──────────────────────────────────────────

class FakeRC522Chip:
    """Register-level MFRC522 emulator: answers REQA/anticollision when a card is present."""

    def __init__(self, uid=None, version=0x92):
        self.regs = {rc522.VERSION: version, rc522.TX_CONTROL: 0x80}
        self.fifo, self.uid = [], uid

    def xfer2(self, data):
        addr, value = (data[0] >> 1) & 0x3F, data[1]
        if data[0] & 0x80:                                      # read
            if addr == rc522.FIFO_DATA:
                return [0, self.fifo.pop(0) if self.fifo else 0]
            if addr == rc522.FIFO_LEVEL:
                return [0, len(self.fifo)]
            return [0, self.regs.get(addr, 0)]
        if addr == rc522.FIFO_DATA:
            self.fifo.append(value)
        elif addr == rc522.FIFO_LEVEL and value & 0x80:
            self.fifo.clear()
        elif addr == rc522.COM_IRQ:
            self.regs[addr] = 0
        else:
            self.regs[addr] = value
        if addr == rc522.BIT_FRAMING and value & 0x80 and self.regs.get(rc522.COMMAND) == rc522.CMD_TRANSCEIVE:
            self._transceive()
        return [0, 0]

    def _transceive(self):
        sent, self.fifo = self.fifo, []
        if self.uid is None:
            self.regs[rc522.COM_IRQ] = 0x01                     # timer: nobody answered
            return
        if sent == [rc522.PICC_REQA]:
            self.fifo = [0x04, 0x00]                            # ATQA
        elif sent == [rc522.PICC_ANTICOLL_CL1, 0x20]:
            u = self.uid
            self.fifo = u + [u[0] ^ u[1] ^ u[2] ^ u[3]]
        self.regs[rc522.COM_IRQ] = 0x30
        self.regs[rc522.CONTROL] = 0


def test_rc522_reads_uid():
    chip = FakeRC522Chip(uid=[0xDE, 0xAD, 0xBE, 0xEF])
    reader = rc522.RC522(spi=chip)
    assert chip.regs[rc522.TX_CONTROL] & 0x03 == 0x03          # antenna switched on
    assert reader.version() == 0x92
    assert reader.read_uid() == "DEADBEEF"


def test_rc522_no_card_and_bad_bcc():
    assert rc522.RC522(spi=FakeRC522Chip(uid=None)).read_uid() is None
    chip = FakeRC522Chip(uid=[1, 2, 3, 4])
    reader = rc522.RC522(spi=chip)
    orig = chip._transceive

    def corrupt():
        orig()
        if len(chip.fifo) == 5:
            chip.fifo[4] ^= 0xFF
    chip._transceive = corrupt
    assert reader.read_uid() is None


# ── node health ────────────────────────────────────────────────────

PMIC = """     3V7_WL_SW_A current(0)=0.10000000A
     3V3_SYS_A current(1)=0.05000000A
    VDD_CORE_A current(7)=2.00000000A
     3V7_WL_SW_V volt(8)=3.70000000V
     3V3_SYS_V volt(9)=3.30000000V
    VDD_CORE_V volt(15)=0.80000000V
      EXT5V_V volt(24)=5.12000000V
       BATT_V volt(25)=0.00000000V
"""


def test_parse_pmic_sums_matching_rails():
    power, ext5v = sysmon_driver.parse_pmic(PMIC)
    assert power == pytest.approx(0.1 * 3.7 + 0.05 * 3.3 + 2.0 * 0.8, abs=0.01)
    assert ext5v == 5.12
    assert sysmon_driver.parse_pmic("") == (None, None)


@pytest.mark.parametrize("text,expected", [
    ("throttled=0x0", {"undervolt": 0, "throttled": 0, "undervolt_boot": 0}),
    ("throttled=0x50005", {"undervolt": 1, "throttled": 1, "undervolt_boot": 1}),
    ("throttled=0x50000", {"undervolt": 0, "throttled": 0, "undervolt_boot": 1}),
    ("error", {}),
])
def test_parse_throttled(text, expected):
    assert sysmon_driver.parse_throttled(text) == expected


def test_sysmon_read_once_from_fake_system(tmp_path, monkeypatch):
    (tmp_path / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
    (tmp_path / "sys/class/thermal/thermal_zone0/temp").write_text("51234\n")
    fan = tmp_path / "sys/devices/platform/cooling_fan/hwmon/hwmon3"
    fan.mkdir(parents=True)
    (fan / "fan1_input").write_text("2950\n")
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/meminfo").write_text("MemTotal: 8000000 kB\nMemFree: 1 kB\nMemAvailable: 6000000 kB\n")
    (tmp_path / "proc/stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    monkeypatch.setattr(sysmon_driver, "SYS", str(tmp_path))
    monkeypatch.setattr(sysmon_driver, "vcgencmd", lambda *a: PMIC if a[0] == "pmic_read_adc" else "throttled=0x0")
    first, prev = sysmon_driver.read_once()
    (tmp_path / "proc/stat").write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")   # 100 busy of 200
    payload, _ = sysmon_driver.read_once(prev)
    assert payload["sensor"] == "sysmon"
    assert payload["cpu_temp"] == 51.2 and payload["fan_rpm"] == 2950
    assert payload["mem_pct"] == 25.0 and payload["cpu_load"] == 50.0
    assert payload["supply_v"] == 5.12 and payload["undervolt"] == 0
    assert "cpu_load" not in first                             # needs two samples


# ── BMS ────────────────────────────────────────────────────────────

def test_soc_register_decoding():
    assert soc_from_bytes(87, 128) == 87.5
    assert soc_from_bytes(103, 0) == 100.0                     # gauges overshoot when full


class FakeSMBus:
    def read_i2c_block_data(self, addr, reg, n):
        assert (addr, reg, n) == (0x36, 0x04, 2)
        return [64, 64]


def test_bms_reads_gauge_without_solar():
    payload = bms_read_once(FuelGauge(0x36, bus=FakeSMBus()), None)
    assert payload["battery_pct"] == 64.2 and "solar_w" not in payload


# ── simulator ──────────────────────────────────────────────────────

def test_simulator_compute_node_is_a_pi5_with_sysmon_and_bms():
    import config
    import sensor_sim
    node = next(n for n in config.NODES if n["type"] == "compute")
    assert node["id"] == "node-compute" and "Pi 5" in node["hardware"]
    st = sensor_sim.SensorState(node["id"])
    env = sensor_sim.build_power_payload(node, st, 0)
    env.update(sensor_sim.build_sysmon_payload(node, st, 0))
    by_sensor = {p["sensor"]: p for _, p in sensor_sim.build_sensor_readings(node, env, 1)}
    assert set(by_sensor) == {"sysmon", "bms"}
    assert {"cpu_temp", "power_w", "fan_rpm", "undervolt"} <= set(by_sensor["sysmon"])
    assert not any(n["type"] == "jetson" for n in config.NODES)


# ── bring-up tool ──────────────────────────────────────────────────

class ScanBus:
    def __init__(self, present):
        self.present, self.reads, self.quick = set(present), [], []

    def read_byte(self, a):
        self.reads.append(a)
        if a not in self.present:
            raise OSError(121, "Remote I/O error")

    def write_quick(self, a):
        self.quick.append(a)
        if a not in self.present:
            raise OSError(121, "Remote I/O error")


def test_i2c_scan_uses_i2cdetect_probe_rules():
    bus = ScanBus({0x36, 0x57, 0x62, 0x76})
    assert bringup.i2c_scan(bus) == [0x36, 0x57, 0x62, 0x76]
    assert 0x57 in bus.reads and 0x36 in bus.reads            # read-probe ranges
    assert 0x62 in bus.quick and 0x57 not in bus.quick


def test_check_values():
    res = dict((m, (s, d)) for m, s, d in bringup.check_values(
        [{"temp": 22.1, "hum": 45}, {"temp": 22.3, "hum": 140}], {"temp": (-10, 60), "hum": (0, 100), "pres": (300, 1100)}))
    assert res["temp"][0] == "ok" and "22.1 … 22.3" in res["temp"][1]
    assert res["hum"][0] == "bad" and res["pres"][0] == "missing"


def test_run_driver_collects_readings_and_errors(tmp_path):
    script = tmp_path / "drv.py"
    script.write_text(
        "import json, sys, time\n"
        "print(json.dumps({'error': 'SCD40 read: CRC mismatch'}), file=sys.stderr, flush=True)\n"
        "for i in range(10):\n"
        "    print(json.dumps({'sensor': 'x', 'v': i}), flush=True); print('noise', flush=True); time.sleep(0.05)\n")
    readings, errors = bringup.run_driver([sys.executable, str(script)], dict(os.environ), 3, 10)
    assert [r["v"] for r in readings] == [0, 1, 2]
    assert errors == ["SCD40 read: CRC mismatch"]


def test_run_driver_times_out_on_silent_driver(tmp_path):
    script = tmp_path / "silent.py"
    script.write_text("import time\ntime.sleep(30)\n")
    readings, _ = bringup.run_driver([sys.executable, str(script)], dict(os.environ), 3, 0.5)
    assert readings == []


def test_bringup_env_file_skips_secrets(tmp_path):
    f = tmp_path / "edge.env"
    f.write_text('IMM_NODE_ID=node-compute\nMQTT_PASSWORD=hunter2\nIMM_EDGE_CLIENT_SECRET="x y"\nINA219_ADDRESS=0x41\n')
    env = bringup.load_env_file(str(f))
    assert env == {"IMM_NODE_ID": "node-compute", "INA219_ADDRESS": "0x41"}


def test_bringup_sensor_table_points_at_real_drivers(monkeypatch):
    monkeypatch.setenv("BMS_GAUGE_ADDRESS", "none")
    table = bringup.sensors()
    assert table["bms"].i2c == []
    for name, s in table.items():
        assert (ROOT / s.driver).is_file(), name


def test_serial_console_detection():
    assert bringup.serial_console_on("/dev/ttyAMA0", "console=ttyAMA0,115200 console=tty1")
    assert bringup.serial_console_on("/dev/serial0", "console=serial0,115200")
    assert not bringup.serial_console_on("/dev/ttyAMA0", "console=tty1 root=PARTUUID=1")


def test_bringup_all_only_runs_connected_sensors(monkeypatch, capsys):
    table = bringup.sensors()
    monkeypatch.setattr(bringup, "i2c_scan", lambda bus: [0x76, 0x62])
    monkeypatch.setattr(bringup, "open_i2c", lambda: None)
    monkeypatch.setattr(bringup.os.path, "exists", lambda p: False)
    ran = []
    monkeypatch.setattr(bringup, "bringup", lambda name, s, c, py: ran.append(name) or (0 if name == "bme280" else 1))
    rc = bringup.bringup_all(table, 1, "python3")
    assert set(ran) == {"bme280", "scd40", "sysmon"}       # sysmon needs no bus; MQ-7 UART absent
    out = capsys.readouterr().out
    assert "✗ scd40" in out and "· mq7" in out and '--sensors "bme280_driver.py"' in out
    assert rc == 1
