"""systemd watchdog: the notify protocol, and that every service the units start keeps kicking."""
import glob
import os
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
import watchdog  # noqa: E402


@pytest.fixture
def notify(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(0.2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setattr(watchdog, "_last", 0.0)
    monkeypatch.setattr(watchdog, "_sock", None)

    def received():
        out = []
        while True:
            try:
                out.append(s.recv(256).decode())
            except socket.timeout:
                return out
    yield received
    s.close()


def test_kick_sends_watchdog_rate_limited(notify, monkeypatch):
    watchdog.kick()
    watchdog.kick()                            # within MIN_INTERVAL_S: dropped
    assert notify() == ["WATCHDOG=1"]
    monkeypatch.setattr(watchdog, "_last", 0.0)
    watchdog.status("reading bme280")
    watchdog.kick()
    assert notify() == ["STATUS=reading bme280", "WATCHDOG=1"]


def test_helper_threads_cannot_hide_a_hung_main_loop(notify):
    t = threading.Thread(target=watchdog.kick)
    t.start()
    t.join()
    assert notify() == []


def test_no_systemd_no_op(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    watchdog.kick()                            # must not raise
    assert not watchdog.enabled()


def test_long_sleep_keeps_kicking(notify, monkeypatch):
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    t = [0.0]
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: t.__setitem__(0, t[0] + 2.0) or t[0])
    watchdog.sleep(12)
    assert len(notify()) >= 3


def test_every_reading_kicks(notify, monkeypatch):
    import mqtt_publisher
    monkeypatch.setattr(mqtt_publisher, "stamp", lambda p, t: (p, t))
    pub = mqtt_publisher.make_publisher("stdout", "habitat/sensors/bme280/zone_a")
    pub({"sensor": "bme280", "temp": 22.0})
    assert "WATCHDOG=1" in notify()


def test_a_hung_script_stops_kicking(tmp_path, notify):
    """End to end with a fake systemd: a loop that kicks, then hangs."""
    code = ("import sys, time; sys.path.insert(0, %r); import watchdog\n"
            "for i in range(3):\n    watchdog.kick(); watchdog._last = 0; time.sleep(0.05)\n"
            "time.sleep(1.0)\n" % os.path.join(ROOT, "core"))
    p = subprocess.Popen([sys.executable, "-c", code], env={**os.environ})
    time.sleep(0.6)
    got = notify()
    p.wait(5)
    assert got == ["WATCHDOG=1"] * 3        # then silence while it "hangs": systemd would restart it


# ── every service the units start must kick ────────────────────────
def deployed_scripts():
    out = sorted(glob.glob(os.path.join(ROOT, "sensor_drivers", "*.py")))
    out += sorted(glob.glob(os.path.join(ROOT, "eclss", "*.py")))
    out += sorted(glob.glob(os.path.join(ROOT, "eva", "*.py")))
    return [p for p in out if not os.path.basename(p).startswith("_")]


@pytest.mark.parametrize("path", deployed_scripts(), ids=lambda p: os.path.relpath(p, ROOT))
def test_every_deployed_script_kicks_the_watchdog(path):
    src = open(path).read()
    kicks = ("watchdog.kick(" in src or "watchdog.sleep(" in src or "make_publisher(" in src
             or "open_tag_reader(" in src)        # the tag readers kick while waiting
    assert kicks, f"{os.path.relpath(path, ROOT)} runs under a WatchdogSec unit but never kicks"


@pytest.mark.parametrize("unit", ["imm-sensor-pipeline@.service", "imm-eclss@.service", "imm-eva@.service",
                                  "imm-lighting-controller.service"])
def test_units_have_watchdog_and_restart_forever(unit):
    s = open(os.path.join(ROOT, "systemd", unit)).read()
    wd = int(re.search(r"^WatchdogSec=(\d+)", s, re.M).group(1))
    assert "NotifyAccess=all" in s and "Restart=always" in s and "StartLimitIntervalSec=0" in s
    assert s.index("StartLimitIntervalSec=0") < s.index("[Service]")      # a [Unit] setting
    assert 30 <= wd <= 300
