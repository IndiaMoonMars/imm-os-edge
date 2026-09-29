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
    assert out["habitat/sensors/gnss/exterior"] == {"sensor": "gnss", "timestamp": 1.0, "zone": "exterior", "fix": 0, "sats": 2}


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


def test_zone_is_not_replaced_by_the_nodes_zone(monkeypatch):
    import mqtt_publisher
    monkeypatch.setenv("IMM_ZONE", "zone_a")
    topic, payload = eb.to_payloads(LINE, 1.0, "exterior")[0]
    out, t = mqtt_publisher.stamp(payload, topic)
    assert t.endswith("/exterior") and out["zone"] == "exterior"


# ── Boards with their own dashboard firmware ──────────────────────────

def test_flat_json_of_another_firmware():
    line = eb.recognise({"CPM": 21, "uSv/h": 0.14, "latitude": "19.07601 N", "longitude": "72.87765 E",
                         "satellites": 8, "altitude": 14.2, "speed_kmh": 1.852, "RSSI": -61, "uptime": 99})
    assert line["geiger"] == {"cpm": 21.0, "usv_h": 0.14}
    g = line["gnss"]
    assert (g["lat"], g["lon"], g["sats"], g["fix"], g["alt_m"]) == (19.07601, 72.87765, 8.0, 1, 14.2)
    assert abs(g["sog_kn"] - 1.0) < 1e-9 and line["board"] == {"rssi_dbm": -61.0, "uptime_s": 99.0}
    out = dict(eb.to_payloads(line, 1.0, "exterior"))
    assert out["habitat/sensors/gnss/exterior"]["sats"] == 8 and isinstance(out["habitat/sensors/gnss/exterior"]["sats"], int)


def test_nested_json_hemispheres_and_nmea():
    line = eb.recognise({"gps": {"lat": 3354.12, "latDir": "S", "lng": 15112.6, "ew": "E", "sats": 5},
                         "geiger": {"usvh": 0.2}})
    g = line["gnss"]
    assert abs(g["lat"] - -33.902) < 1e-6 and abs(g["lon"] - 151.21) < 1e-6
    assert line["geiger"]["cpm"] == round(0.2 * 153.8, 1)


def test_no_fix_hides_zero_position():
    g = eb.recognise({"lat": 0.0, "lon": 0.0, "satellites": 2, "cpm": 30})["gnss"]
    assert g == {"sats": 2.0, "fix": 0}


def test_page_with_values_as_text():
    page = """<html><head><style>b{color:red}</style><script>var x = 1;</script></head><body>
      <h1>Radiation &amp; GPS</h1><p>CPM: <b>27</b></p><p>Dose (uSv/h): 0.18</p>
      <div>Latitude: 19.076010</div><div>Longitude: 72.877650</div><div>Satellites: 7</div></body></html>"""
    line = eb.recognise(eb.parse_body(page))
    assert line["geiger"]["cpm"] == 27.0 and line["geiger"]["usv_h"] == 0.18
    assert line["gnss"]["lat"] == 19.07601 and line["gnss"]["sats"] == 7.0


def test_map_names_it_cannot_guess():
    m = eb.parse_map("cpm=r.k, lat=p.y, lon=p.x")
    line = eb.recognise({"r": {"k": 40}, "p": {"y": 19.1, "x": 72.9}}, m)
    assert line["geiger"]["cpm"] == 40.0 and line["gnss"]["lat"] == 19.1 and line["gnss"]["fix"] == 1


def test_probe_finds_the_data_url_a_page_fetches():
    pages = {"http://b/": "<html><script>setInterval(()=>fetch('/readings').then(r=>r.json()),1000)</script>"
                          "<span id=cpm>--</span></html>",
             "http://b/readings": '{"cpm": 19, "lat": 19.07, "lon": 72.87, "sats": 6}'}

    def get(u):
        if u in pages:
            return pages[u]
        raise urllib.error.URLError("404")
    lines = []
    assert eb.probe("http://b/", get=get, out=lines.append) == "http://b/readings"
    assert any("fetch" in ln or "loads data from: /readings" in ln for ln in lines)


def test_find_scans_the_network():
    def get(u):
        if u.startswith("http://10.0.0.7/"):
            return '{"cpm": 19}'
        raise urllib.error.URLError("no")
    found = eb.find(["10.0.0.5", "10.0.0.7"], get=get, out=lambda s: None, port_open=lambda h: True)
    assert found == ["http://10.0.0.7/"]


def test_polling_a_board_without_values_says_so(capsys):
    eb.run_http("http://b/", lambda p, t: None, "exterior", now=lambda: 1.0, sleep=lambda s: None,
                fetch=lambda u: {"title": "hello"}, max_loops=5)
    assert "--probe" in capsys.readouterr().err
