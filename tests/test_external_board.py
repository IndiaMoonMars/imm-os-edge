"""sensor_drivers/external_board_bridge.py: the GNSS + Geiger board's lines → IMM-OS topics."""
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sensor_drivers"))
sys.path.insert(0, os.path.join(ROOT, "core"))
import external_board_bridge as eb  # noqa: E402

LINE = {"ms": 61000, "geiger": {"cpm": 18.0, "usv_h": 0.117, "counts": 1203, "window_s": 60, "warming": 0},
        "gnss": {"fix": 1, "sats": 9, "lat": 19.076, "lon": 72.8777, "alt_m": 14.2, "sog_kn": 0.1, "cog_deg": 0,
                 "utc": "2026-09-29T08:30:01Z"},
        "board": {"uptime_s": 61, "reset_reason": 1, "boot_count": 3, "i2c_err": 0, "rssi_dbm": -58}}


def test_sections_become_streams():
    out = dict(eb.to_payloads(LINE, 1790670000.0, "exterior"))
    assert set(out) == {"habitat/sensors/geiger/exterior", "habitat/sensors/gnss/exterior", "habitat/sensors/board/exterior"}
    g = out["habitat/sensors/gnss/exterior"]
    assert g["lat"] == 19.076 and g["sats"] == 9 and isinstance(g["fix"], int) and g["gnss_utc"] == "2026-09-29T08:30:01Z"
    assert out["habitat/sensors/geiger/exterior"]["usv_h"] == 0.117


def test_no_fix_publishes_satellite_count_only():
    out = dict(eb.to_payloads({"gnss": {"fix": 0, "sats": 2}}, 1.0, "exterior"))
    assert out["habitat/sensors/gnss/exterior"] == {"sensor": "gnss", "timestamp": 1.0, "fix": 0, "sats": 2}


def test_wifi_polling_skips_repeats_and_survives_outages():
    lines = [LINE, LINE, urllib.error.URLError("timed out"), {**LINE, "ms": 62000}]

    def fetch(url):
        v = lines.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    sent = []
    eb.run_http("http://x/json", lambda p, t: sent.append(t), "exterior", now=lambda: 1.0, sleep=lambda s: None,
                fetch=fetch, max_loops=4)
    assert len(sent) == 6                        # two distinct lines × three streams; the repeat and the outage add nothing
