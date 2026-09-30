"""core/sd_recorder.py: every reading as CSV on the Pi's SD card, filed by mission sol."""
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
import sd_recorder as sd  # noqa: E402

T0 = 1790457496.0                     # Sun 27 Sep 2026, 02:48:16 IST
MISSION = {"id": 1, "name": "Analog Mission Alpha", "start": T0, "sols": 7, "ended_at": None,
           "start_ist": "Sun 27 Sep 2026, 02:48:16 IST"}


def read(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def test_folders_follow_the_mission_sols_in_ist():
    assert sd.folder_for(T0 + 5, MISSION) == os.path.join("analog-mission-alpha-1", "sol-01")
    assert sd.folder_for(T0 + 2 * 86400 + 3600, MISSION) == os.path.join("analog-mission-alpha-1", "sol-03")
    assert sd.folder_for(T0 - 3600, MISSION) == os.path.join("analog-mission-alpha-1", "pre-mission", "2026-09-27")
    assert sd.folder_for(T0 + 7 * 86400 + 1, MISSION) == os.path.join("no-mission", "2026-10-04")
    assert sd.folder_for(T0 + 86400 * 2, {**MISSION, "ended_at": T0 + 86400}) == os.path.join("no-mission", "2026-09-29")
    assert sd.folder_for(T0, None) == os.path.join("no-mission", "2026-09-27")


def test_readings_become_csv_rows_with_fixed_columns(tmp_path):
    rec = sd.Recorder(str(tmp_path), disk_free=lambda p: 10e9, now=lambda: T0)
    rec.set_mission(MISSION)
    warm = {"sensor": "mq4", "timestamp": T0 + 60.25, "node_id": "node-rpi-01", "zone": "zone_a", "seq": 1, "run": "r",
            "vout_mv": 1240.0, "rs_rl": 3.03, "warming": 1, "warm_left_s": 120, "calibrated": 1}
    p = rec.record("habitat/sensors/mq4/zone_a", warm)
    rec.record("habitat/sensors/mq4/zone_a", {**warm, "timestamp": T0 + 400, "warming": 0, "ch4_ppm": 4.1,
                                              "warm_left_s": None, "new_metric": 7})
    rec.flush()
    assert p == os.path.join(str(tmp_path), "analog-mission-alpha-1", "sol-01", "mq4_zone_a.csv")
    rows = read(p)
    assert rows[0] == ["time_ist", "time_utc", "node_id", "zone", "ch4_ppm", "rs_r0", "vout_mv", "rs_rl", "warming",
                       "warm_left_s", "calibrated", "extra"]
    assert rows[1][:4] == ["2026-09-27 02:49:16.250", "2026-09-26T21:19:16.250Z", "node-rpi-01", "zone_a"]
    assert rows[1][4] == "" and rows[1][8] == "1" and rows[1][9] == "120"
    assert rows[2][4] == "4.1" and json.loads(rows[2][-1]) == {"new_metric": 7}    # ch4 appears after warm-up: own column
    rec.close()


def test_restart_appends_under_the_same_header_and_remembers_the_mission(tmp_path):
    rec = sd.Recorder(str(tmp_path), disk_free=lambda p: 10e9, now=lambda: T0)
    rec.set_mission(MISSION)
    rec.record("habitat/sensors/geiger/exterior", {"sensor": "geiger", "timestamp": T0 + 10, "zone": "exterior", "cpm": 18.0, "usv_h": 0.117})
    rec.close()
    again = sd.Recorder(str(tmp_path), disk_free=lambda p: 10e9, now=lambda: T0)     # MCC not asked yet
    assert again.mission == MISSION
    p = again.record("habitat/sensors/geiger/exterior", {"sensor": "geiger", "timestamp": T0 + 11, "zone": "exterior", "cpm": 19.0})
    again.close()
    rows = read(p)
    assert len(rows) == 3 and rows[0].count("time_ist") == 1 and rows[2][4] == "19.0"
    assert os.path.exists(os.path.join(str(tmp_path), "README.txt"))


def test_unknown_sensor_gets_columns_from_its_first_reading(tmp_path):
    rec = sd.Recorder(str(tmp_path), disk_free=lambda p: 10e9, now=lambda: T0)
    p = rec.record("habitat/sensors/newthing/lab", {"sensor": "newthing", "timestamp": T0, "a": 1, "b": 2})
    rec.close()
    assert read(p)[0] == ["time_ist", "time_utc", "node_id", "zone", "a", "b", "extra"]
    assert rec.record("habitat/sensors/x/y", {"sensor": "x"}) is None                   # no timestamp: not a reading


def test_pauses_when_the_card_is_nearly_full_and_resumes(tmp_path):
    free = {"b": 10e9}
    rec = sd.Recorder(str(tmp_path), min_free_mb=1024, disk_free=lambda p: free["b"], now=lambda: T0)
    free["b"] = 500e6
    rec.flush()
    assert rec.paused and rec.record("habitat/sensors/bme280/zone_a", {"sensor": "bme280", "timestamp": T0, "temp": 24}) is None
    free["b"] = 1400e6
    rec.flush()
    assert not rec.paused and rec.record("habitat/sensors/bme280/zone_a", {"sensor": "bme280", "timestamp": T0, "temp": 24})
    rec.close()


def test_idle_files_are_closed_and_old_ones_pruned_only_when_asked(tmp_path):
    clock = {"t": T0}
    rec = sd.Recorder(str(tmp_path), keep_days=0, disk_free=lambda p: 10e9, now=lambda: clock["t"])
    p = rec.record("habitat/sensors/bme280/zone_a", {"sensor": "bme280", "timestamp": T0, "temp": 24})
    clock["t"] += 700
    rec.flush()
    assert rec.files == {} and os.path.exists(p)                                   # closed, kept
    os.utime(p, (T0 - 40 * 86400, T0 - 40 * 86400))
    rec.keep_days = 30
    rec.flush()
    assert not os.path.exists(p)


def test_mission_clock_polling_keeps_the_cache_when_the_mcc_is_away(tmp_path):
    import threading
    rec = sd.Recorder(str(tmp_path), disk_free=lambda p: 10e9, now=lambda: T0)
    stop = threading.Event()
    answers = [MISSION, RuntimeError("MCC down")]

    def fetch(url):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def sleep(s):
        if not answers:
            stop.set()
    sd.mission_loop(rec, "http://imm.local/api/mission/clock", fetch=fetch, sleep=sleep, stop=stop)
    assert rec.mission == MISSION
