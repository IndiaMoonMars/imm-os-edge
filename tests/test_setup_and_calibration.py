"""Calibration, env-file editing and the MAX30100 driver (with a fake I2C bus)."""
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── calibration ────────────────────────────────────────────────────

def test_calibration_applies_and_reloads(tmp_path, monkeypatch):
    from calibration import Calibration, CalibrationError, validate
    f = tmp_path / "cal.yaml"
    f.write_text("bme280:\n  temp: {offset: -0.8}\n  hum: {gain: 1.1, offset: -2}\n")
    cal = Calibration(str(f))
    out = cal.apply({"sensor": "bme280", "temp": 23.6, "hum": 40.0, "pres": 1000.0})
    assert out["temp"] == pytest.approx(22.8) and out["hum"] == pytest.approx(42.0) and out["pres"] == 1000.0
    assert cal.apply({"sensor": "scd40", "co2_ppm": 500}) == {"sensor": "scd40", "co2_ppm": 500}
    f.write_text("bme280:\n  temp: {offset: -1.0}\n")
    os.utime(f, (1, 2))                                    # force a new mtime
    assert cal.correct("bme280", "temp", 23.0) == pytest.approx(22.0)
    f.write_text("bme280: [broken\n")
    os.utime(f, (3, 4))
    assert cal.correct("bme280", "temp", 23.0) == pytest.approx(22.0)   # bad edit keeps last good file
    monkeypatch.setenv("IMM_CALIBRATION_OFF", "1")
    assert cal.correct("bme280", "temp", 23.0) == 23.0
    with pytest.raises(CalibrationError):
        validate({"o2": {"o2_pct": {"gain": 5}}})
    with pytest.raises(CalibrationError):
        validate({"o2": {"o2_pct": {"gian": 1}}})
    with pytest.raises(CalibrationError):
        validate({"o2": {"o2_pct": {"offset": True}}})


def test_calibrate_cli_two_point_and_report(tmp_path):
    f = str(tmp_path / "cal.yaml")
    run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "tools", "calibrate.py"), "--file", f, *a],
                                    capture_output=True, text=True)
    r = run("two-point", "bme280.hum", "--raw", "31.0,72.9", "--true", "33.0,75.3", "--reference", "salt", "--by", "pratham")
    assert r.returncode == 0, r.stderr
    from calibration import Calibration
    assert Calibration(f).correct("bme280", "hum", 31.0) == pytest.approx(33.0, abs=1e-3)
    assert Calibration(f).correct("bme280", "hum", 72.9) == pytest.approx(75.3, abs=1e-3)
    assert run("set", "o2.o2_pct", "--gain", "3", "--reference", "x").returncode != 0      # rejected, not saved
    assert "| bme280 | hum |" in run("report").stdout
    assert run("remove", "bme280.hum").returncode == 0 and "no corrections" in run("show").stdout


def test_publisher_applies_calibration(tmp_path, monkeypatch):
    f = tmp_path / "cal.yaml"
    f.write_text("scd40:\n  co2_ppm: {offset: 12}\n")
    monkeypatch.setenv("IMM_CALIBRATION_FILE", str(f))
    import calibration
    monkeypatch.setattr(calibration, "_default", None)
    import mqtt_publisher
    out, _ = mqtt_publisher.stamp({"sensor": "scd40", "co2_ppm": 600, "timestamp": 1}, "habitat/sensors/scd40/zone_a")
    assert out["co2_ppm"] == 612


# ── edge.env editing ───────────────────────────────────────────────

def test_envfile_updates_in_place_and_quotes(tmp_path):
    from envfile import main as envfile
    f = tmp_path / "edge.env"
    f.write_text("# comment\nIMM_NODE_ID=old\nMQTT_PORT=1883\nIMM_NODE_ID=dup\n")
    assert envfile([str(f), "IMM_NODE_ID=node-rpi-01", 'LIGHT_ZONES=core:0,1;galley:2,3', 'MQTT_PASSWORD=a$b"c']) == 0
    text = f.read_text()
    assert text.splitlines()[:3] == ["# comment", "IMM_NODE_ID=node-rpi-01", "MQTT_PORT=1883"]
    assert text.count("IMM_NODE_ID") == 1
    out = subprocess.run(["bash", "-c", f'. "{f}"; printf "%s|%s" "$LIGHT_ZONES" "$MQTT_PASSWORD"'],
                         capture_output=True, text=True).stdout
    assert out == 'core:0,1;galley:2,3|a$b"c'


def _setup_dry(tmp_path, *args):
    """setup-node.sh --dry-run as the current user, with /etc/imm-os redirected to tmp_path."""
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    (conf / "mqtt-ca.crt").write_text("already installed")   # so --ca isn't required
    env = dict(os.environ, IMM_CONF_DIR=str(conf), IMM_HOSTS_FILE=str(tmp_path / "hosts"),
               IMM_MODEL_FILE=str(tmp_path / "model"), IMM_BOOT_CONFIG=str(tmp_path / "config.txt"))
    env.pop("IMM_EDGE_CLIENT_SECRET", None)
    env.pop("MQTT_PASSWORD", None)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    return subprocess.run(["bash", os.path.join(ROOT, "scripts", "setup-node.sh"), "--dry-run", "--user", user,
                           "--node-id", "node-rpi-01", "--zone", "zone_a", "--mcc-ip", "192.168.1.20", *args],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)


def test_setup_reads_secrets_file(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.write_text('IMM_EDGE_CLIENT_SECRET=ab$cd e"f\r\nMQTT_PASSWORD=p$w d\r\n')   # CRLF from Windows
    out = _setup_dry(tmp_path, "--secrets-file", str(secrets))
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"secrets read from {secrets}" in out.stdout
    assert secrets.exists()                     # a dry run deletes nothing


def test_setup_adds_mcc_to_cloud_init_hosts_template(tmp_path, monkeypatch):
    """cloud-init rewrites /etc/hosts at boot; the entry must be in its template too."""
    tpl = tmp_path / "templates"
    tpl.mkdir()
    (tpl / "hosts.debian.tmpl").write_text("## template:jinja\n127.0.0.1 localhost\n")
    (tmp_path / "hosts").write_text("127.0.0.1 localhost\n")
    secrets = tmp_path / "secrets"
    secrets.write_text("IMM_EDGE_CLIENT_SECRET=a\nMQTT_PASSWORD=b\n")
    monkeypatch.setenv("IMM_CLOUD_HOSTS_TEMPLATES", str(tpl))
    out = _setup_dry(tmp_path, "--secrets-file", str(secrets))
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"append '192.168.1.20 imm.local' to {tmp_path / 'hosts'}" in out.stdout
    assert f"append '192.168.1.20 imm.local' to {tpl / 'hosts.debian.tmpl'}" in out.stdout


def test_setup_secrets_file_must_have_both(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.write_text("IMM_EDGE_CLIENT_SECRET=abc\n")
    out = _setup_dry(tmp_path, "--secrets-file", str(secrets))
    assert out.returncode != 0
    assert "MQTT_PASSWORD missing" in out.stderr
    missing = _setup_dry(tmp_path, "--secrets-file", str(tmp_path / "nope"))
    assert missing.returncode != 0 and "not found" in missing.stderr


# ── MAX30100 driver ────────────────────────────────────────────────

class FakeMax30100Bus:
    """Register-level fake: 16-sample FIFO with write/read pointers."""

    def __init__(self, part=0x11, samples=()):
        self.regs = {0xFF: part, 0x02: 0, 0x03: 0, 0x04: 0}
        self.fifo = list(samples)
        self.writes = []

    def read_byte_data(self, addr, reg):
        if reg == 0x02:
            return len(self.fifo) & 0x0F
        return self.regs.get(reg, 0)

    def write_byte_data(self, addr, reg, value):
        self.writes.append((reg, value))
        self.regs[reg] = value & ~0x40 if reg == 0x06 else value   # RESET bit self-clears, like the chip

    def read_i2c_block_data(self, addr, reg, n):
        assert reg == 0x05 and n <= 32
        out = []
        for _ in range(n // 4):
            ir, red = self.fifo.pop(0)
            out += [ir >> 8, ir & 0xFF, red >> 8, red & 0xFF]
        return out


def install_fake_bus(monkeypatch, bus):
    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda n: bus))


def test_max30100_configures_and_reads_fifo_in_order(monkeypatch):
    samples = [(40000 + i, 30000 + i) for i in range(11)]
    bus = FakeMax30100Bus(samples=samples)
    install_fake_bus(monkeypatch, bus)
    from max30100 import MAX30100
    dev = MAX30100()
    dev.enable_spo2()
    assert (0x07, 0x47) in bus.writes and (0x06, 0x03) in bus.writes     # 100 Hz, 1600 µs; SpO2 mode
    got = dev.read_fifo()
    assert got == samples and (dev.ir, dev.red) == samples[-1]             # 11 samples: two block reads
    assert dev.read_fifo() == []


def test_max30100_rejects_max30102(monkeypatch):
    install_fake_bus(monkeypatch, FakeMax30100Bus(part=0x15))
    from max30100 import MAX30100
    with pytest.raises(RuntimeError, match="MAX30102"):
        MAX30100()
