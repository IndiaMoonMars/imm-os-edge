"""tools/verify_esp32.py: the ESP32 board's automatic checks and hands-on tests (no hardware)."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import verify_esp32 as v  # noqa: E402


def line(**over):
    """One healthy board line (a room in Mumbai); over = {"o2": {"o2_pct": 18.25}, "mq4": None, ...}."""
    d = {"bme280": {"temp": 24.0, "hum": 46.6, "pres": 1007.8},
         "scd40": {"co2_ppm": 543, "temp": 25.9, "hum": 41.0},
         "o2": {"o2_pct": 20.9},
         "bno055": {"heading_deg": 182.0, "roll_deg": 0.4, "pitch_deg": -1.2, "lin_acc_ms2": 0.03, "imu_calib": 3,
                    "grav_ms2": 9.81, "mag_ut": 42.0, "gyro_dps": 0.1, "temp": 26, "calib_gyro": 3, "calib_acc": 3,
                    "calib_mag": 3},
         "mq4": {"vout_mv": 930, "rs_rl": 4.376, "warming": 0, "calibrated": 0}}
    for k, val in over.items():
        if val is None:
            d.pop(k)
        else:
            d[k] = {**d[k], **val}
    return d


def samples(n=30, t0=100.0, **over):
    return [(t0 + i, line(**over)) for i in range(n)]


def by(checks, sensor, name):
    return next(c for c in checks if c.sensor == sensor and c.name == name)


NOTES = ["sensors: bme280=0x76 scd40=0x62 bno055=0x28 o2=found mq4_r0=0.000 divider=2.00",
         "bno055 self-test: accel=pass mag=pass gyro=pass mcu=pass"]


def test_healthy_board_has_no_failures():
    checks = v.auto_checks(samples(), NOTES)
    assert not [c for c in checks if c.status == "FAIL"], [c for c in checks if c.status == "FAIL"]
    assert by(checks, "scd40", "dew point vs BME280").status == "OK"       # 24.0/46.6 vs 25.9/41.0: both ~12 °C
    assert by(checks, "bno055", "self-test").status == "OK"
    assert by(checks, "mq4", "calibration").status == "WARN"                # no CAL_MQ4 yet


def test_low_o2_with_normal_co2_is_a_calibration_problem():
    """The board's first real run: O₂ 18.25 % while CO₂ is 543 ppm."""
    c = by(v.auto_checks(samples(o2={"o2_pct": 18.25}), NOTES), "o2", "O₂")
    assert c.status == "FAIL" and "20.9 % O₂" in c.detail and "CAL_O2" in c.hint
    # stale air: O₂ and CO₂ agree, so it's the room, not the sensor
    c = by(v.auto_checks(samples(o2={"o2_pct": 20.2}, scd40={"co2_ppm": 6500}), NOTES), "o2", "O₂")
    assert c.status == "WARN" and "ventilate" in c.hint


def test_humidity_sensors_disagree():
    c = by(v.auto_checks(samples(scd40={"hum": 70.0}), NOTES), "scd40", "dew point vs BME280")
    assert c.status == "FAIL"


def test_missing_sensor_and_self_test_failure():
    checks = v.auto_checks(samples(mq4=None), [NOTES[0], "bno055 self-test: accel=pass mag=FAIL gyro=pass mcu=pass"])
    bme = by(v.auto_checks(samples(bme280=None), NOTES + ["bme280: no reading (data read failed (I2C))"]), "bme280", "present")
    assert bme.status == "FAIL" and bme.detail == "no readings; board says: bme280: no reading (data read failed (I2C))"
    assert by(checks, "mq4", "present").status == "FAIL" and "GPIO32" in by(checks, "mq4", "present").hint
    st = by(checks, "bno055", "self-test")
    assert st.status == "FAIL" and st.detail == "failed: mag"


def test_imu_physics():
    checks = v.auto_checks(samples(bno055={"mag_ut": 120.0, "grav_ms2": 8.1, "imu_calib": 1}), NOTES)
    assert by(checks, "bno055", "magnetic field").status == "FAIL"
    assert by(checks, "bno055", "gravity").status == "FAIL"
    assert by(checks, "bno055", "calibration").status == "WARN"
    assert by(v.auto_checks(samples(bno055={"mag_ut": 80.0}), NOTES), "bno055", "magnetic field").status == "WARN"


def test_co2_below_outdoor_air_is_impossible():
    assert by(v.auto_checks(samples(scd40={"co2_ppm": 300}), NOTES), "scd40", "CO₂").status == "FAIL"
    assert by(v.auto_checks(samples(scd40={"co2_ppm": 1400}), NOTES), "scd40", "CO₂").status == "WARN"


def test_breath_test():
    before = samples(15, 0)
    after = [(20 + i, line(scd40={"co2_ppm": 543 + 60 * i, "hum": 41 + i}, bme280={"hum": 46.6 + 0.8 * i},
                           o2={"o2_pct": 20.9 - 0.02 * i})) for i in range(10)]
    res = {(c.sensor, c.name): c.status for c in v.judge_breath(before, after)}
    assert res == {("scd40", "breath: CO₂ rises"): "OK", ("bme280", "breath: humidity rises"): "OK",
                   ("scd40", "breath: humidity rises"): "OK", ("o2", "breath: O₂ dips"): "OK"}
    assert v.PASS_EARLY["breath"](before, after)
    flat = v.judge_breath(before, samples(10, 20))
    assert [c.status for c in flat if c.sensor != "o2"] == ["FAIL", "FAIL", "FAIL"]


def test_turn_handles_heading_wrap_around():
    before = samples(10, 0, bno055={"heading_deg": 350.0})
    after = [(20 + i, line(bno055={"heading_deg": (350 + 10 * i) % 360})) for i in range(10)]   # 350 → 80
    c = v.judge_turn(before, after)[0]
    assert c.status == "OK" and c.detail == "90° change"


def test_tilt_and_gas():
    before = samples(10, 0)
    after = [(20 + i, line(bno055={"pitch_deg": -1.2 + 10 * i, "gyro_dps": 35.0})) for i in range(10)]
    assert [c.status for c in v.judge_tilt(before, after)] == ["OK", "OK", "OK"]
    assert v.judge_gas(before, [(20, line(mq4={"vout_mv": 1500}))])[0].status == "OK"
    assert v.judge_gas(before, [(20, line(mq4={"vout_mv": 960}))])[0].status == "FAIL"


def test_table_lists_every_value():
    out = v.table(samples())
    for label in ("Pressure", "Dew point", "CO₂", "O₂", "Gravity", "Magnetic field", "Rotation rate",
                  "Chip temperature", "Calibration: magnetometer", "Rs/RL", "Warming up"):
        assert label in out


def test_against_the_simulated_board():
    env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "tools", "hwsim"), "ESP32_PORT": "/dev/ttyUSB0"}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "verify_esp32.py"), "--auto", "--seconds", "6"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Magnetic field" in r.stdout and "self-test" in r.stdout and '"fail": 0' in r.stdout
