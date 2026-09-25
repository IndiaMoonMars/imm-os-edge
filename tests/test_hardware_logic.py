"""Pure-logic tests for the ECLSS/EVA hardware scripts (no hardware or network needed)."""
import json
import math
import random

import pytest

from biometrics import heart_rate, spo2
from positioning import nmea_checksum_ok, parse_dwm_lec, parse_nmea
from tag_readers import decode_keys

FS = 100


def nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


def ppg(bpm, seconds=6, noise=60, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(FS * seconds):
        ph = (i / FS * bpm / 60) % 1.0
        pulse = -900 * math.exp(-((ph - 0.15) / 0.06) ** 2) - 250 * math.exp(-((ph - 0.45) / 0.05) ** 2)
        out.append(50000 + pulse + 300 * math.sin(2 * math.pi * 0.25 * i / FS) + rnd.gauss(0, noise))
    return out


# ── biometrics ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bpm", [45, 60, 72, 100, 140, 180])
def test_heart_rate_within_3_bpm_despite_dicrotic_notch(bpm):
    assert heart_rate(ppg(bpm), FS) == pytest.approx(bpm, abs=3)


def test_heart_rate_none_without_signal():
    assert heart_rate([300] * 600, FS) is None                                    # no finger
    rnd = random.Random(2)
    assert heart_rate([50000 + rnd.gauss(0, 300) for _ in range(600)], FS) is None  # noise only
    assert heart_rate(ppg(70)[:200], FS) is None                                  # too short


@pytest.mark.parametrize("r,expected", [(0.5, 97.5), (0.8, 90.0)])
def test_spo2_ratio_of_ratios(r, expected):
    ir = [40000 + 400 * math.sin(i / 5) for i in range(200)]
    red = [30000 + r * 400 * 30000 / 40000 * math.sin(i / 5) for i in range(200)]
    assert spo2(red, ir) == pytest.approx(expected, abs=0.2)


def test_spo2_none_on_bad_contact():
    assert spo2([100] * 200, [100] * 200) is None


# ── positioning ────────────────────────────────────────────────────

def test_nmea_rmc_gga_multi_constellation():
    rmc = parse_nmea(nmea("GNRMC,123519.00,A,1258.2960,N,07735.6760,E,0.4,84.4,250926,,,A"))
    gga = parse_nmea(nmea("GNGGA,123519.00,1258.2960,N,07735.6760,E,1,12,0.8,920.5,M,-86.0,M,,"))
    assert rmc["lat"] == pytest.approx(12.9716) and rmc["lon"] == pytest.approx(77.5946)
    assert gga["satellites"] == 12 and gga["alt_m"] == 920.5


def test_nmea_rejects_bad_checksum_no_fix_and_other_sentences():
    good = nmea("GPRMC,1,A,3351.000,S,15112.000,W,0,0,1,,")
    assert parse_nmea(good)["lat"] < 0 and parse_nmea(good)["lon"] < 0
    assert not nmea_checksum_ok(good[:-2] + "00")
    assert parse_nmea(good[:-2] + "00") is None
    assert parse_nmea(nmea("GNRMC,1,V,,,,,,,,,,N")) is None
    assert parse_nmea(nmea("GNGGA,1,,,,,0,0,,,M,,M,,")) is None
    assert parse_nmea(nmea("GNGSV,3,1,11")) is None


def test_dwm1001_lec_pos_block():
    line = "DIST,2,AN0,1151,5.00,8.00,2.25,6.48,AN1,0CA8,0.00,8.00,2.25,6.51,POS,1.23,4.56,0.87,85"
    assert parse_dwm_lec(line) == {"x_m": 1.23, "y_m": 4.56, "z_m": 0.87, "quality": 85}
    assert parse_dwm_lec("DIST,1,AN0,1151,5.00,8.00,2.25,6.48") is None
    assert parse_dwm_lec("POS,nan,nan,nan,0") is None


# ── HID key decoding (barcode / RFID readers) ─────────────────────

def test_decode_keys_with_shift_and_repeats():
    ev = [("KEY_LEFTSHIFT", 1), ("KEY_T", 1), ("KEY_T", 0), ("KEY_LEFTSHIFT", 0), ("KEY_O", 1), ("KEY_O", 2),
          ("KEY_MINUS", 1), ("KEY_0", 1), ("KEY_1", 1), ("KEY_ENTER", 1), ("KEY_ENTER", 1), ("KEY_A", 1), ("KEY_ENTER", 1)]
    assert list(decode_keys(ev)) == ["To-01", "a"]


# ── ECLSS event logic ──────────────────────────────────────────────

def test_flow_events_split_on_idle_and_ignore_drips():
    from water_monitor import FlowEventTracker
    t = FlowEventTracker(pulses_per_l=450, idle_s=5, min_ml=20)
    assert t.update(225, 0) is None          # 0.5 L so far
    assert t.update(450, 1) is None          # 1.0 L
    assert t.update(450, 4) is None          # idle 3 s: still the same draw
    assert t.update(450, 6.1) == pytest.approx(1000.0)
    assert t.update(455, 10) is None         # a 5-pulse drip (11 mL)…
    assert t.update(455, 16) is None         # …is below FLOW_MIN_ML: no event


def test_shower_session_ignores_short_motion():
    from shower_timer import ShowerSession
    s = ShowerSession(idle_s=90, min_s=30)
    assert s.update(True, 0) is None and s.update(False, 50) is None
    assert s.update(False, 100) is None                        # 0 s of motion: ignored
    for t in range(200, 380, 5):
        assert s.update(True, t) is None
    assert s.update(False, 400) is None
    assert s.update(False, 470) == pytest.approx(175.0)        # motion 200→375, ended after 90 s idle


def test_waste_deposits_need_settled_increase():
    from waste_tracker import DepositDetector
    d = DepositDetector(min_kg=0.02, tolerance=0.01, settle=3)
    for w in (2.00, 2.00, 2.00):
        assert d.update(w) is None                             # baseline
    for w in (2.40, 2.10, 2.35):
        assert d.update(w) is None                             # bag still swinging
    assert [d.update(w) for w in (2.30, 2.30, 2.30)] == [None, None, pytest.approx(0.30)]
    assert [d.update(w) for w in (0.10, 0.10, 0.10)] == [None, None, None]   # emptied: new baseline
    assert [d.update(w) for w in (0.60, 0.60, 0.60)][-1] == pytest.approx(0.50)


def test_climate_hysteresis_and_min_times():
    from eclss_pid import Hysteresis
    h = Hysteresis(setpoint=22, band=1, min_on_s=180, min_off_s=180)
    assert h.decide(22.9, 0) is False                          # inside the band
    assert h.decide(23.2, 200) is True                         # above 23: cool
    assert h.decide(20.5, 250) is True                         # below 21, but min on-time not reached
    assert h.decide(20.5, 400) is False
    assert h.decide(24.0, 450) is False                        # min off-time protects the compressor
    assert h.decide(24.0, 600) is True


def test_climate_readings_follow_own_node_and_prefer_bme280():
    from eclss_pid import LatestReadings
    r = LatestReadings("node-rpi-01")
    r.feed({"sensor": "bme280", "node_id": "node-rpi-02", "temp": 30}, 0)
    assert r.snapshot()[0] is None                             # other node ignored
    r.feed({"sensor": "scd40", "node_id": "node-rpi-01", "temp": 23.0, "hum": 48}, 1)
    r.feed({"sensor": "bme280", "node_id": "node-rpi-01", "temp": 22.0, "hum": 45}, 2)
    r.feed({"sensor": "scd40", "node_id": "node-rpi-01", "temp": 23.5, "hum": 49}, 3)
    assert r.snapshot()[:2] == (22.0, 45)                      # BME280 wins while it's alive
    r.feed({"sensor": "scd40", "node_id": "node-rpi-01", "temp": 24.0, "hum": 50}, 70)
    assert r.snapshot()[:2] == (24.0, 50)                      # BME280 silent > 60 s: fall back


def test_climate_fail_safe_turns_relays_off_on_stale_data():
    import threading
    from eclss_pid import Hysteresis, control_loop
    from hw import LogRelay
    hvac, dehum = LogRelay("HVAC"), LogRelay("Dehum")
    hvac.set(True)
    t, h = Hysteresis(22, 1, 0, 0), Hysteresis(50, 5, 0, 0)
    t.on = True
    stop = threading.Event()
    calls = []

    def stale():
        calls.append(1)
        if len(calls) > 1:
            stop.set()
        return 30.0, 70.0, -1e6          # last reading ages ago

    control_loop(stale, hvac, dehum, t, h, stale_s=120, period_s=0, stop=stop)
    assert hvac.is_on is False and dehum.is_on is False


def test_biolab_parsers():
    from biolab_monitor import parse_ds18b20, parse_ezo_response
    assert parse_ds18b20("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125") == 23.125
    assert parse_ds18b20("72 01 : crc=00 NO\n72 01 t=23125") is None
    assert parse_ds18b20("50 05 : crc=aa YES\n50 05 t=85000") is None         # power-on reset value
    assert parse_ezo_response(b"\x017.02\x00\x00\x00") == 7.02
    assert parse_ezo_response(b"\x02") is None                                # syntax error status
    assert parse_ezo_response(b"\xfe") is None                                # still processing


def test_lighting_mix_and_zone_map():
    from lighting_controller import mix, parse_zones
    assert mix(100, 2700) == (1.0, 0.0) and mix(100, 6500) == (0.0, 1.0)
    w, c = mix(50, 4600)
    assert w == pytest.approx(c) and w + c == pytest.approx(0.5 ** 2.2, rel=1e-3)
    assert mix(0, 5000) == (0.0, 0.0)
    assert parse_zones("core:0,1; galley:2,3") == {"core": (0, 1), "galley": (2, 3)}


# ── EVA tool station and spooling ──────────────────────────────────

def test_tool_station_toggle_persists(tmp_path):
    from tool_tracker import ToolState
    path = tmp_path / "out.json"
    s = ToolState("toggle", str(path))
    assert s.action_for("TOOL-001") == "CHECKOUT"
    s.record("TOOL-001", "CHECKOUT")
    assert ToolState("toggle", str(path)).action_for("TOOL-001") == "CHECKIN"   # survives restart
    assert ToolState("checkout", str(path)).action_for("TOOL-001") == "CHECKOUT"


def test_event_poster_spools_when_offline_and_flushes_in_order(tmp_path, monkeypatch):
    import hw
    sent, online = [], [False]

    def fake_send(self, payload):
        if not online[0]:
            return "retry"
        sent.append(payload["n"])
        return "drop" if payload["n"] == 99 else "ok"

    monkeypatch.setattr(hw.EventPoster, "_send", fake_send)
    p = hw.EventPoster("http://x", "t", spool_dir=str(tmp_path))
    assert p.post({"n": 1}) == "spooled" and p.post({"n": 2}) == "spooled"
    assert [json.loads(x)["n"] for x in p.spool.read_text().splitlines()] == [1, 2]
    online[0] = True
    assert p.post({"n": 3}) == "ok"
    assert sent == [1, 2, 3] and not p.spool.exists()
    assert p.post({"n": 99}) == "rejected"                     # 4xx: dropped, not spooled
    assert not p.spool.exists()
