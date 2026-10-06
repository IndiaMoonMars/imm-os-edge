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
bnox 480 368 -240 16 -32 8 0 30 980 -3
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
    assert last["bno055"] == {"heading_deg": 90.0, "roll_deg": -5.0, "pitch_deg": 2.0, "lin_acc_ms2": 0.5, "imu_calib": 3,
                              "grav_ms2": 9.80,             # |(0, 0.30, 9.80)| m/s²
                              "mag_ut": 40.7,               # |(30, 23, -15)| µT
                              "gyro_dps": 2.29,             # |(1, -2, 0.5)| °/s
                              "temp": -3, "calib_gyro": 3, "calib_acc": 3, "calib_mag": 3, "cal_restored": 0}
    assert "# bno055 self-test: accel=pass mag=pass gyro=pass mcu=pass" in notes
    assert last["o2"]["o2_pct"] == pytest.approx(20.9 / 120 * 110.5, abs=0.01)   # default key when uncalibrated
    # 620 mV × 2.0 divider; Rs/RL = (5000 - 1240) / 1240; no ppm yet
    assert last["mq4"] == {"vout_mv": 1240, "rs_rl": 3.032, "warming": 1, "calibrated": 0, "warm_left_s": 172}


def test_bno055_self_test_and_calibration_parts(sim, tmp_path):
    data, notes = sim("bnost 13\n" + BASE.replace("0 255", "0 54") + "run 2\n", tmp_path)   # mag failed; 0b00110110
    assert "# bno055 self-test: accel=pass mag=FAIL gyro=pass mcu=pass" in notes
    b = data[-1]["bno055"]
    assert (b["imu_calib"], b["calib_gyro"], b["calib_acc"], b["calib_mag"]) == (0, 3, 1, 2)


def test_scd40_every_5_s_with_crc_checked_words(sim, tmp_path):
    data, _ = sim(BASE + "run 12\n", tmp_path)
    scd = [d["scd40"] for d in data if "scd40" in d]
    assert len(scd) == 2                                                          # one per 5 s measurement
    assert scd[0] == {"co2_ppm": 612, "temp": pytest.approx(-45 + 175 * 26000 / 65535, abs=0.01),
                      "hum": pytest.approx(100 * 26214 / 65535, abs=0.01), "asc": 1}       # factory: self-calibration on


def test_missing_sensor_left_out_then_found(sim, tmp_path):
    data, notes = sim("remove bno\n" + BASE + "run 5\nadd bno\nrun 30\n", tmp_path)
    assert "bno055=none" in next(n for n in notes if n.startswith("# sensors:"))
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


def test_bme280_settings_written_again_if_ignored_at_start(sim, tmp_path):
    """A BME280 still starting up ignores writes: the firmware reads ctrl_meas back and retries."""
    data, notes = sim("bmeignore 2\n" + BASE + "run 3\n", tmp_path)
    assert not any("did not stick" in n for n in notes)
    assert data[-1]["bme280"]["temp"] == 25.08


def test_bme280_that_never_measures_is_explained_and_reinitialised(sim, tmp_path):
    script = "bmeignore 100\n" + BASE.replace("bme 519888 415148 30000", "bme 524288 524288 32768") + "run 12\n"
    data, notes = sim(script, tmp_path)
    assert any("settings did not stick" in n for n in notes)
    why = [n for n in notes if n.startswith("# bme280: no reading")]
    assert why and "no measurement (ctrl_meas=0x00" in why[0]                    # the reason, with the register
    assert "# bme280: re-initialising" in notes
    assert all("bme280" not in d for d in data) and "bno055" in data[-1]          # the rest keeps reporting


def test_bme280_keeps_reporting_through_chip_resets(sim, tmp_path):
    """On the real board the BME280 kept losing its settings (ctrl_meas 0x00: a power-on reset
    from a supply dip) and, in normal mode, never measured again. Forced mode sends them with
    every reading, so a reset costs nothing, and the resets are counted as evidence."""
    script = BASE + "run 3\nbmereset\nrun 2\nbmereset\nrun 2\nsend STATUS\nrun 1\n"
    data, notes = sim(script, tmp_path)
    assert all(d["bme280"]["temp"] == 25.08 for d in data)                       # every second, no gap
    assert sum("chip reset" in n and "since start (settings lost" in n for n in notes) == 1   # said once
    assert "# bme280: chip reset 2 time(s) since start" in notes                 # STATUS keeps the count
    assert not any("no reading" in n for n in notes)


def test_bme280_in_normal_mode_would_have_stopped(sim, tmp_path):
    """The simulator's reset really clears the chip: a measurement is needed to get data back."""
    data, _ = sim(BASE + "run 2\n", tmp_path)
    assert data[-1]["bme280"]["temp"] == 25.08
    data, notes = sim(BASE + "run 2\nbmeignore 1000\nbmereset\nrun 3\n", tmp_path)   # reset, then writes ignored
    assert "bme280" not in data[-1]
    assert any("no measurement (ctrl_meas=0x00" in n for n in notes)


def test_scd40_co2_zero_is_left_out(sim, tmp_path):
    data, notes = sim(BASE.replace("scd 612 26000 26214", "scd 0 26000 26214") + "run 12\n", tmp_path)
    scd = [d["scd40"] for d in data if "scd40" in d]
    assert scd and all("co2_ppm" not in s for s in scd) and scd[0]["temp"] == pytest.approx(24.43, abs=0.01)
    assert sum("scd40: CO2 reads 0" in n for n in notes) == 1                    # said once, not every reading


def test_board_health_reset_reason_and_boot_count(sim, tmp_path):
    data, notes = sim("resetreason 9\n" + BASE + "run 21\nreset\nrun 1\n", tmp_path)
    boards = [d["board"] for d in data if "board" in d]
    assert boards[0] == {"uptime_s": boards[0]["uptime_s"], "reset_reason": 9, "boot_count": 1, "i2c_err": 0, "bme_resets": 0}
    assert len(boards) >= 3                                       # every 10 s, and in the first line after a boot
    assert boards[-1]["boot_count"] == 2                          # counted in flash across the reset
    assert any("BROWNOUT" in n for n in notes)                    # STATUS at boot explains reason 9


def test_task_watchdog_is_fed_through_normal_work(sim, tmp_path):
    # includes a re-probe for a missing sensor and a bus recovery: the slowest things the loop does
    data, notes = sim("remove bno\n" + BASE + "run 35\ni2cstuck 5\nrun 10\nadd bno\nrun 35\nwdt\n", tmp_path)
    gap, timeout, added = map(int, next(n for n in notes if n.startswith("WDT")).split()[1:])
    assert added == 1 and timeout == 10000 and gap < timeout / 2


def test_stuck_i2c_bus_is_recovered_and_sensors_come_back(sim, tmp_path):
    data, notes = sim(BASE + "run 3\ni2cstuck 5\nrun 12\n", tmp_path)
    assert any("bus recovered" in n for n in notes)
    after = data[-1]
    assert {"bme280", "o2", "bno055"} <= set(after)               # readings again after the recovery
    assert after["board"]["i2c_err"] >= 20 if "board" in after else True
    errs = [d["board"]["i2c_err"] for d in data if "board" in d]
    assert errs[-1] >= 20


def test_o2_reports_whether_it_was_calibrated(sim, tmp_path):
    data, _ = sim(BASE + "run 2\n", tmp_path)                    # key register 0: factory default
    assert data[-1]["o2"]["calibrated"] == 0
    data, _ = sim(BASE.replace("o2 0 110 5 0", "o2 190 110 0 0") + "run 2\n", tmp_path)
    assert data[-1]["o2"]["calibrated"] == 1


def test_missing_sensor_probes_are_not_i2c_errors(sim, tmp_path):
    data, _ = sim("remove bno\nremove o2\n" + BASE + "run 65\n", tmp_path)     # two re-probes
    assert [d["board"]["i2c_err"] for d in data if "board" in d][-1] == 0


# ── Warm-up only where physics needs it ─────────────────────────────

def test_mq4_warm_up_countdown_and_none_after_a_reset_that_kept_the_heater_on(sim, tmp_path):
    data, notes = sim(BASE + "run 3\n", tmp_path)                    # power-on
    assert "# mq4: heater warming up for 3 min" in notes
    first, last = data[0]["mq4"], data[-1]["mq4"]
    assert first["warming"] == 1 and 178 <= first["warm_left_s"] <= 180
    assert last["warm_left_s"] == first["warm_left_s"] - 2
    for reason in (6, 4, 3, 2):                                     # watchdog, crash, software, EN button
        data, notes = sim(BASE + f"run 200\nresetreason {reason}\nreset\nrun 2\n", tmp_path)
        assert "# mq4: heater stayed powered through this reset: no warm-up" in notes
        assert data[-1]["mq4"]["warming"] == 0 and "warm_left_s" not in data[-1]["mq4"]
    data, _ = sim(BASE + "run 200\nresetreason 9\nreset\nrun 2\n", tmp_path)   # brownout: the heater cooled
    assert data[-1]["mq4"]["warming"] == 1


def test_mq4_calibration_right_after_a_watchdog_reset(sim, tmp_path):
    data, notes = sim("resetreason 6\n" + BASE + "run 2\nsend CAL_MQ4\nrun 12\n", tmp_path)
    assert any(n.startswith("# CAL_MQ4: R0 stored") for n in notes)
    assert "ch4_ppm" in data[-1]["mq4"]


def test_bno055_calibration_stored_and_restored_after_restart(sim, tmp_path):
    offsets = " ".join(str(v) for v in range(10, 32))                # 22 bytes the fusion found
    data, notes = sim(BASE + f"bnoset {offsets}\nrun 3\nresetreason 6\nreset\nbnooffsets\nrun 2\n", tmp_path)
    assert sum(n == "# bno055: fully calibrated: calibration stored, restored at every start" for n in notes) == 2  # once per start
    restored = next(n for n in notes if n.startswith("BNOOFF")).split()[1:]
    assert restored == offsets.split()                               # written back after the chip's own reset
    assert data[-1]["bno055"]["cal_restored"] == 1
    data, notes = sim(BASE + "run 2\nsend CAL_BNO_CLEAR\nrun 1\n", tmp_path)
    assert "# CAL_BNO_CLEAR: stored BNO055 calibration forgotten" in notes


def test_bno055_not_stored_until_fully_calibrated(sim, tmp_path):
    data, notes = sim(BASE.replace("0 255", "0 254") + "run 5\nreset\nrun 1\n", tmp_path)   # magnetometer 2/3
    assert not any("calibration stored" in n for n in notes)
    assert data[-1]["bno055"]["cal_restored"] == 0


def test_scd40_forced_recalibration_turns_self_calibration_off(sim, tmp_path):
    _, notes = sim(BASE + "run 10\nsend CAL_CO2\nrun 1\n", tmp_path)
    assert "# CAL_CO2: let the SCD40 run 3 min in fresh air first" in notes
    data, notes = sim(BASE + "run 185\nsend CAL_CO2 430\nrun 12\nscdstate\nsend CAL_CO2 90\nrun 1\n", tmp_path)
    assert "# CAL_CO2: SCD40 set to 430 ppm (correction -182 ppm); automatic self-calibration off" in notes  # it read 612
    assert "SCD asc=0 persisted=0 frc=430 running=1 resets=0" in notes        # stored in the sensor; measuring again
    assert [d["scd40"] for d in data if "scd40" in d][-1]["asc"] == 0
    assert any("ppm must be 400-2000" in n for n in notes)
    _, notes = sim(BASE + "run 185\nsend CAL_CO2\nrun 2\nsend ASC_ON\nrun 6\nscdstate\n", tmp_path)
    assert "# ASC_ON: SCD40 automatic self-calibration on" in notes
    assert "SCD asc=1 persisted=1 frc=420 running=1 resets=0" in notes


def test_scd40_zero_co2_is_explained_and_self_test_tells_fault_from_supply(sim, tmp_path):
    zero = BASE.replace("scd 612 ", "scd 0 ")
    data, notes = sim(zero + "run 70\n", tmp_path)
    assert all("co2_ppm" not in d.get("scd40", {}) for d in data)
    assert any("temp" in d.get("scd40", {}) for d in data)             # temperature and humidity still published
    assert any("CO2 still reads 0" in n and "SCD_TEST" in n for n in notes)
    _, notes = sim(zero + "run 6\nsend SCD_TEST\nrun 7\nscdstate\n", tmp_path)
    assert any(n.startswith("# SCD_TEST: passed") and "supply" in n for n in notes)
    assert any(n.startswith("SCD asc=1") and "running=1" in n for n in notes)   # measuring again afterwards
    _, notes = sim(zero + "scdselftest 5\nrun 6\nsend scd_test\nrun 1\n", tmp_path)
    assert any(n.startswith("# SCD_TEST: FAILED (code 0x0005)") for n in notes)


def test_scd40_factory_reset_forgets_forced_recalibration(sim, tmp_path):
    _, notes = sim(BASE + "run 185\nsend CAL_CO2 430\nrun 2\nsend SCD_RESET\nrun 6\nscdstate\n", tmp_path)
    assert any(n.startswith("# SCD_RESET: SCD40 back to factory settings") for n in notes)
    assert "SCD asc=1 persisted=1 frc=0 running=1 resets=1" in notes


def test_scd_off_disables_the_sensor_is_remembered_and_scd_on_resumes(sim, tmp_path):
    data, notes = sim(BASE + "run 7\nsend SCD_OFF\nrun 3\nsend SCD_ON\nrun 45\n", tmp_path)
    assert any(n.startswith("# scd40: disabled and remembered") for n in notes)
    assert any(n.startswith("# scd40: enabled") for n in notes)
    assert any("scd40" in d for d in data[:10])                # present before SCD_OFF (every 5 s)
    assert any("scd40" in d for d in data[-8:])                # back after SCD_ON + a re-probe (5 s cadence)
    # remembered across a restart (stored in NVS): the board does not look for it after a reset
    _, notes = sim(BASE + "run 1\nsend SCD_OFF\nrun 1\nresetreason 1\nreset\nrun 1\nsend STATUS\nrun 1\n", tmp_path)
    assert any("scd40=none" in n for n in notes)


def test_self_heal_recovers_then_reboots_when_all_i2c_sensors_go_silent(sim, tmp_path):
    # a dead device holding the shared bus silences every I2C sensor while the board keeps running
    _, notes = sim(BASE + "run 5\nremove bme\nremove scd\nremove bno\nremove o2\nrun 130\nreboots\n", tmp_path)
    assert any(n.startswith("# self-heal: no I2C sensor data; recovering the bus") for n in notes)
    assert any(n.startswith("# self-heal: I2C sensors still silent; rebooting") for n in notes)
    assert "REBOOTS 1" in notes                                # recover first, then one reboot


def test_self_heal_leaves_a_board_with_a_working_sensor_alone(sim, tmp_path):
    # removing only the faulty SCD40 (the real fix) must NOT trigger a reboot: the others still read
    _, notes = sim(BASE + "run 5\nremove scd\nrun 130\nreboots\n", tmp_path)
    assert not any("self-heal" in n for n in notes)
    assert "REBOOTS 0" in notes


# ── Wi-Fi ───────────────────────────────────────────────────────────

def test_wifi_from_usb_commands_serves_json_and_page(sim, tmp_path):
    data, notes = sim(BASE + "run 1\nwifi\nsend wifi_ssid Habitat Net 2\nsend WIFI_PASS pA$$ w0rd\nrun 1\nwifi\n"
                      "send status\nrun 11\nhttp /json\nhttp /\n", tmp_path)
    assert "WIFI [] [] 0" in notes                                    # USB only until told otherwise
    assert "WIFI [Habitat Net 2] [pA$$ w0rd] 1" in notes              # name with spaces, password case kept
    assert "# wifi: connected ip=192.168.1.77" in notes
    j = next(n for n in notes if n.startswith("HTTP 200 application/json"))
    served = json.loads(j.split(" ", 3)[3])
    assert served == data[-1]                                         # the same line as on USB
    assert "bme280" in served
    assert [d["board"].get("rssi_dbm") for d in data if "board" in d][-1] == -58   # Wi-Fi signal in board health
    assert any(n.startswith("HTTP 200 text/html") and "IMM-OS sensor board" in n for n in notes)


def test_mdns_announces_the_board_by_name_once_wifi_is_up(sim, tmp_path):
    # imm-sensors.local lets the Pi find the board on any router without a reserved IP
    _, notes = sim(BASE + "run 1\nmdns\nsend WIFI_SSID Net\nsend WIFI_PASS pw\nrun 1\nmdns\n", tmp_path)
    assert "MDNS  0" in notes                                              # nothing announced before Wi-Fi
    assert "# mdns: reachable as http://imm-sensors.local/json (the name works on any router)" in notes
    assert "MDNS imm-sensors 1" in notes                                   # announced once connected


def test_wifi_remembered_across_restart_and_forgotten(sim, tmp_path):
    _, notes = sim(BASE + "run 1\nsend WIFI_SSID Hab\nsend WIFI_PASS secret\nrun 1\nresetreason 6\nreset\nrun 1\nwifi\n"
                   "send WIFI_OFF\nrun 1\nwifi\n", tmp_path)
    assert "WIFI [Hab] [secret] 1" in notes and "# wifi: connecting (STATUS shows the IP)" in notes
    assert "# wifi: network forgotten; USB only" in notes and notes[-1].startswith("WIFI") and notes[-1].endswith(" 0")


def test_commands_still_case_insensitive(sim, tmp_path):
    _, notes = sim(BASE + "run 1\nsend cal_o2\nrun 1\n", tmp_path)
    assert "# CAL_O2: SEN0322 set to 20.9 % (fresh air)" in notes
