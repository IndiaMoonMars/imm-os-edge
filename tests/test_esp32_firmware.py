"""The ESP32 sensor-board firmware, compiled for the PC and run against simulated sensors."""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW = os.path.join(ROOT, "firmware", "esp32-sensors")

# test/sim.cpp's BME280 calibration (datasheet example T/P, typical humidity trimming)
T1, T2, T3 = 27504, 26435, -1000
P1, P2, P3, P4, P5, P6, P7, P8, P9 = 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000
H1, H2, H3, H4, H5, H6 = 75, 362, 0, 313, 50, 30
BASE = """mq4 620
bme 519888 415148 30000
scd 612 26000 26214
bno 1440 -80 32 30 -40 0 255
o2 0 110 5 0
"""


def bme280_reference(adc_t, adc_p, adc_h):
    """Bosch's floating-point compensation (datasheet), independent of the firmware's integer code."""
    v1 = (adc_t / 16384.0 - T1 / 1024.0) * T2
    v2 = ((adc_t / 131072.0 - T1 / 8192.0) ** 2) * T3
    t_fine = v1 + v2
    p1 = t_fine / 2.0 - 64000.0
    p2 = p1 * p1 * P6 / 32768.0 + p1 * P5 * 2.0
    p2 = p2 / 4.0 + P4 * 65536.0
    p1 = (P3 * p1 * p1 / 524288.0 + P2 * p1) / 524288.0
    p1 = (1.0 + p1 / 32768.0) * P1
    p = 1048576.0 - adc_p
    p = (p - p2 / 4096.0) * 6250.0 / p1
    p = p + (P9 * p * p / 2147483648.0 + p * P8 / 32768.0 + P7) / 16.0
    h = t_fine - 76800.0
    h = (adc_h - (H4 * 64.0 + H5 / 16384.0 * h)) * (H2 / 65536.0 * (1.0 + H6 / 67108864.0 * h * (1.0 + H3 / 67108864.0 * h)))
    h = min(100.0, max(0.0, h * (1.0 - H1 * h / 524288.0)))
    return t_fine / 5120.0, p / 100.0, h


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("g++ not available")
    exe = str(tmp_path_factory.mktemp("esp32") / "sim")
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(FW, "test", "stub"),
                    "-o", exe, os.path.join(FW, "test", "sim.cpp")], check=True)

    def run(script, tmp_path):
        f = tmp_path / "script.txt"
        f.write_text(script)
        out = subprocess.run([exe, str(f)], capture_output=True, text=True, timeout=60, check=True).stdout
        data = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        notes = [line for line in out.splitlines() if not line.startswith("{")]
        return data, notes
    return run


def test_every_sensor_decoded(sim, tmp_path):
    data, notes = sim(BASE + "run 7\n", tmp_path)
    assert "# sensors: bme280=0x76 scd40=0x62 bno055=0x28 o2=found mq4_r0=0.000 divider=2.00" in notes
    last = data[-1]
    t, p, h = bme280_reference(519888, 415148, 30000)
    assert last["bme280"]["temp"] == pytest.approx(t, abs=0.01)
    assert last["bme280"]["pres"] == pytest.approx(p, abs=0.02)
    assert last["bme280"]["hum"] == pytest.approx(h, abs=0.05)
    assert last["bme280"]["temp"] == 25.08 and last["bme280"]["pres"] == pytest.approx(1006.53, abs=0.01)  # datasheet example
    assert last["bno055"] == {"heading_deg": 90.0, "roll_deg": -5.0, "pitch_deg": 2.0, "lin_acc_ms2": 0.5, "imu_calib": 3}
    assert last["o2"]["o2_pct"] == pytest.approx(20.9 / 120 * 110.5, abs=0.01)   # default key when uncalibrated
    assert last["mq4"] == {"vout_mv": 1240, "warming": 1, "calibrated": 0}        # 620 mV × 2.0 divider; no ppm yet


def test_scd40_every_5_s_with_crc_checked_words(sim, tmp_path):
    data, _ = sim(BASE + "run 12\n", tmp_path)
    scd = [d["scd40"] for d in data if "scd40" in d]
    assert len(scd) == 2                                                          # one per 5 s measurement
    assert scd[0] == {"co2_ppm": 612, "temp": pytest.approx(-45 + 175 * 26000 / 65535, abs=0.01),
                      "hum": pytest.approx(100 * 26214 / 65535, abs=0.01)}


def test_missing_sensor_left_out_then_found(sim, tmp_path):
    data, notes = sim("remove bno\n" + BASE + "run 5\nadd bno\nrun 30\n", tmp_path)
    assert "bno055=none" in notes[1]
    assert "bno055" not in data[0] and "bme280" in data[0]
    assert "bno055" in data[-1]                                                   # re-probed within 30 s


def test_bmp280_is_reported_not_used(sim, tmp_path):
    data, notes = sim("bmp\n" + BASE + "run 2\n", tmp_path)
    assert any("BMP280" in n for n in notes) and "bme280" not in data[-1]


def test_mq4_calibration_and_ppm(sim, tmp_path):
    script = BASE + "run 2\nsend CAL_MQ4\nrun 2\nrun 180\nsend CAL_MQ4\nrun 12\nmq4 310\nrun 2\nreset\nrun 2\n"
    data, notes = sim(script, tmp_path)
    assert any("still warming up" in n for n in notes)
    assert any(n.startswith("# CAL_MQ4: R0 stored") for n in notes)
    rs_rl = (5000 - 1240) / 1240
    r0 = rs_rl / 4.4
    after_cal = [d["mq4"] for d in data if d["mq4"].get("calibrated") == 1 and d["mq4"]["vout_mv"] == 1240]
    assert after_cal[-1]["rs_r0"] == pytest.approx(4.4, abs=0.01)
    assert after_cal[-1]["ch4_ppm"] == pytest.approx(1012.7 * 4.4 ** -2.786, abs=0.1)   # clean air
    gas = [d["mq4"] for d in data if d["mq4"]["vout_mv"] == 620 and d["mq4"]["warming"] == 0][-1]
    assert gas["rs_r0"] == pytest.approx(((5000 - 620) / 620) / r0, abs=0.01)
    # after a reboot R0 comes back from flash, and ppm waits for the warm-up again
    rebooted = data[-1]["mq4"]
    assert rebooted["calibrated"] == 1 and rebooted["warming"] == 1 and "ch4_ppm" not in rebooted


def test_mq4_out_of_range_is_left_out(sim, tmp_path):
    for mv in (5, 3200):                                                          # disconnected / saturated ADC
        data, _ = sim(BASE.replace("mq4 620", f"mq4 {mv}") + "run 2\n", tmp_path)
        assert "mq4" not in data[-1] and "bme280" in data[-1]


def test_o2_calibration_and_commands(sim, tmp_path):
    data, notes = sim(BASE + "run 1\nsend cal_o2\nrun 1\no2user\nsend FOO\nrun 1\n", tmp_path)
    assert "# CAL_O2: SEN0322 set to 20.9 % (fresh air)" in notes
    assert "O2USER 209" in notes
    assert any("unknown command" in n for n in notes)
    data, _ = sim(BASE.replace("o2 0 110 5 0", "o2 190 110 0 0") + "run 2\n", tmp_path)
    assert data[-1]["o2"]["o2_pct"] == pytest.approx(0.190 * 110, abs=0.01)    # stored key
