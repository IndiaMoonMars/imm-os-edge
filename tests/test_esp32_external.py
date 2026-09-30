"""The external sensor-board firmware (GNSS TEL0157 + Geiger SEN0463), compiled for the PC and run
against a simulated receiver, tube, Wi-Fi and serial (firmware/esp32-external/test/sim.cpp)."""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW = os.path.join(ROOT, "firmware", "esp32-external")
FIX = "gnss 2026 9 29 8 30 1 19.076 72.8777 9 14.2\n"


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("g++ not available")
    exe = str(tmp_path_factory.mktemp("ext") / "sim")
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(FW, "test", "stub"),
                    "-o", exe, os.path.join(FW, "test", "sim.cpp")], check=True)

    def run(script, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text(script)
        out = subprocess.run([exe, str(f)], capture_output=True, text=True, timeout=60, check=True).stdout
        return [json.loads(l) for l in out.splitlines() if l.startswith("{")], [l for l in out.splitlines() if not l.startswith("{")]
    return run


def test_gnss_fix_decoded(sim, tmp_path):
    data, _ = sim(FIX + "run 2\n", tmp_path)
    g = data[-1]["gnss"]
    assert g["fix"] == 1 and g["sats"] == 9 and g["utc"] == "2026-09-29T08:30:01Z"
    assert abs(g["lat"] - 19.076) < 1e-5 and abs(g["lon"] - 72.8777) < 1e-5 and g["alt_m"] == 14.2


def test_southern_western_and_below_sea_level(sim, tmp_path):
    data, _ = sim("gnss 2026 9 29 8 30 1 -33.8688 -70.6693 7 -12.5\nrun 2\n", tmp_path)
    g = data[-1]["gnss"]
    assert g["lat"] < 0 and g["lon"] < 0 and g["alt_m"] == -12.5      # DFRobot's library drops this sign


def test_no_fix_reports_satellites_only(sim, tmp_path):
    data, _ = sim("nofix 2\nrun 2\n", tmp_path)
    assert data[-1]["gnss"] == {"fix": 0, "sats": 2}


def test_missing_gnss_then_found(sim, tmp_path):
    data, notes = sim("remove\nrun 3\nadd\nrun 31\n", tmp_path)
    assert "gnss" not in data[0] and any("no TEL0157" in n for n in notes)
    assert "gnss" in data[-1]
    assert [d["board"]["i2c_err"] for d in data if "board" in d][-1] == 0     # looking for it is not an error


def test_geiger_cpm_and_dose_rate(sim, tmp_path):
    data, _ = sim("cpm 30\nrun 30\n", tmp_path)
    assert data[-1]["geiger"]["warming"] == 1                                   # first minute: extrapolated
    data, _ = sim("cpm 30\nrun 125\n", tmp_path)
    g = data[-1]["geiger"]
    assert g["warming"] == 0 and g["window_s"] == 60 and abs(g["cpm"] - 30) <= 1.5
    assert abs(g["usv_h"] - g["cpm"] / 153.8) < 0.002                           # M4011: 153.8 CPM per µSv/h


def test_ringing_counts_once(sim, tmp_path):
    data, _ = sim("run 2\nburst 8\nrun 2\n", tmp_path)
    assert data[-1]["geiger"]["counts"] == 1


def test_wifi_credentials_with_spaces_and_json_endpoint(sim, tmp_path):
    _, notes = sim("run 1\nsend WIFI_SSID Habitat Lab 2G\nsend wifi_pass p@ss word\nrun 2\nwifi\nhttp /json\nhttp /\n", tmp_path)
    assert "WIFI Habitat Lab 2G p@ss word 1" in notes
    body = next(n for n in notes if n.startswith("HTTP 200 application/json"))
    assert json.loads(body.split(" ", 3)[3])["geiger"]
    assert any(n.startswith("HTTP 200 text/html") for n in notes)


def test_board_health_watchdog_and_bus_recovery(sim, tmp_path):
    data, notes = sim("resetreason 9\n" + FIX + "run 12\ni2cstuck 5\nrun 8\nwdt\nreset\nrun 2\n", tmp_path)
    boards = [d["board"] for d in data if "board" in d]
    assert boards[0]["reset_reason"] == 9 and boards[-1]["boot_count"] == 2
    assert any("bus recovered" in n for n in notes) and "gnss" in data[-1]
    gap, timeout = map(int, next(n for n in notes if n.startswith("WDT")).split()[1:])
    assert timeout == 10000 and gap < 1000


def test_bus_stuck_from_boot_is_recovered_when_looking_for_the_gnss(sim, tmp_path):
    data, notes = sim("i2cstuck 5\n" + FIX + "run 3\nrun 30\n", tmp_path)
    assert "gnss" not in data[0] and "gnss" in data[-1] and any("bus recovered" in n for n in notes)
