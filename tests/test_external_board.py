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


def _raw_server(pages):
    """A server like a hand-written ESP32 WiFiServer sketch: no status line, no headers."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            req = c.recv(1024).decode()
            path = req.split(" ")[1] if " " in req else "/"
            c.sendall(pages.get(path, "<html><head></head><body>404</body></html>").encode())
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_board_without_http_headers_is_read():
    page = "<html><head><title>Rad</title></head><body>CPM: 31<br>Satellites: 6<br>Lat: 19.07601<br>Lon: 72.87765</body></html>"
    srv = _raw_server({"/": page})
    try:
        url = f"http://127.0.0.1:{srv.getsockname()[1]}/"
        line = eb.recognise(eb.poll_http(url))
        assert line["geiger"]["cpm"] == 31.0 and line["gnss"]["sats"] == 6.0 and line["gnss"]["lat"] == 19.07601
        assert eb.probe(url, out=lambda s: None) == url
    finally:
        srv.close()


def test_find_survives_odd_devices():
    def get(u):
        if "10.0.0.5" in u:
            raise ValueError("weird")          # e.g. a printer answering nonsense
        if "10.0.0.6" in u:
            return "<html><head><title>Router</title></head></html>"
        return '{"cpm": 19}'
    lines = []
    found = eb.find(["10.0.0.5", "10.0.0.6", "10.0.0.7"], get=get, out=lines.append, port_open=lambda h: True)
    assert found == ["http://10.0.0.7/"]
    assert any('10.0.0.6: web server, "Router"' in ln for ln in lines)


# ── On USB, with the board's own firmware ─────────────────────────────

class FakeSerial:
    def __init__(self, lines):
        self.lines = [ln.encode() for ln in lines]

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


SKETCH_OUTPUT = ["CPM: 24\r\n", "uSv/h: 0.16\r\n", "Lat: 19.076010 Lon: 72.877650\r\n", "Satellites: 8\r\n",
                 "CPM: 25\r\n", "uSv/h: 0.16\r\n", "Lat: 19.076011 Lon: 72.877651\r\n", "Satellites: 8\r\n", ""]


def test_text_prints_are_grouped_into_readings():
    col = eb.LineCollector()
    got = [r for r in (col.feed(ln) for ln in SKETCH_OUTPUT[:-1]) if r] + [col.flush()]
    assert len(got) == 2
    assert got[0]["geiger"]["cpm"] == 24.0 and got[0]["gnss"]["lat"] == 19.07601 and got[0]["gnss"]["sats"] == 8.0
    assert got[1]["geiger"]["cpm"] == 25.0


def test_serial_publishes_the_sketchs_prints():
    sent = []
    eb.run_serial("/dev/x", lambda p, t: sent.append((t, p)), "exterior", now=lambda: 1.0,
                  ser=FakeSerial(SKETCH_OUTPUT), max_lines=len(SKETCH_OUTPUT))
    geiger = [p for t, p in sent if t == "habitat/sensors/geiger/exterior"]
    assert [p["cpm"] for p in geiger] == [24.0, 25.0]
    assert any(t == "habitat/sensors/gnss/exterior" and p["sats"] == 8 for t, p in sent)


def test_serial_still_reads_imm_os_firmware():
    import json
    sent = []
    eb.run_serial("/dev/x", lambda p, t: sent.append(t), "exterior", now=lambda: 1.0,
                  ser=FakeSerial(["# ready\n", json.dumps(LINE) + "\n"]), max_lines=2)
    assert len(sent) == 3


def test_listen_finds_speed_and_values():
    lines = []

    def opener(port, baud):
        if baud == 115200:
            return FakeSerial(["\x8f\x02\xfe\x81garbage\n"] * 3)            # wrong speed looks like noise
        return FakeSerial(SKETCH_OUTPUT)
    baud, reading = eb.listen("/dev/ttyUSB1", seconds=0.2, out=lines.append, opener=opener, users=lambda p: [])
    assert baud == 9600 and reading["geiger"]["cpm"] in (24.0, 25.0)
    assert any("EXT_BOARD_PORT=/dev/ttyUSB1 EXT_BOARD_BAUD=9600" in ln for ln in lines)


def test_listen_recognises_the_internal_board_and_a_busy_port():
    lines = []
    baud, _ = eb.listen("/dev/ttyUSB0", seconds=0.2, out=lines.append, users=lambda p: [],
                        opener=lambda p, b: FakeSerial(['{"bme280": {"temp": 24.1}, "scd40": {"co2_ppm": 600}}\n']))
    assert baud == 0 and any("INTERNAL" in ln for ln in lines)
    assert "  ✓ internal sensor board: ESP32_PORT=/dev/ttyUSB0" in lines      # setup pins it from this line

    def busy(p, b):
        raise OSError("[Errno 11] Could not exclusively lock port /dev/ttyUSB0: Resource temporarily unavailable")
    lines = []
    assert eb.listen("/dev/ttyUSB0", seconds=0.1, out=lines.append, opener=busy, users=lambda p: [])[0] == 0
    assert any("in use by a running service" in ln for ln in lines)


def test_internal_board_never_takes_the_external_boards_port():
    import hw
    ports = ["/dev/serial/by-id/usb-Silicon_Labs_CP2102_A-if00-port0", "/dev/serial/by-id/usb-Silicon_Labs_CP2102_B-if00-port0"]
    find = lambda pat: ports if "by-id" in pat else []          # noqa: E731
    assert hw.esp32_port({"EXT_BOARD_PORT": ports[0]}, find) == ports[1]
    assert hw.esp32_port({}, find) == ports[0]


def test_listen_names_the_program_holding_the_port(tmp_path):
    dev = tmp_path / "ttyUSB1"
    dev.write_text("")
    pid = tmp_path / "proc" / "4242"
    (pid / "fd").mkdir(parents=True)
    (pid / "fd" / "3").symlink_to(dev)
    (pid / "cmdline").write_bytes(b"python3\0sensor_drivers/esp32_bridge.py\0--mode\0both\0")
    assert eb.port_users(str(dev), proc=str(tmp_path / "proc")) == ["4242 python3 sensor_drivers/esp32_bridge.py --mode both"]
    lines = []
    eb.listen(str(dev), out=lines.append, users=lambda p: eb.port_users(p, proc=str(tmp_path / "proc")),
              opener=lambda p, b: FakeSerial([]))
    assert any("INTERNAL board's driver" in ln for ln in lines)


def test_listen_reset_shows_link_works_but_sketch_is_silent():
    class Resettable(FakeSerial):
        def __init__(self):
            super().__init__([])
            self.pins = []

        def __setattr__(self, k, v):
            if k in ("dtr", "rts"):
                self.pins.append((k, v))
                if k == "rts" and v is False and ("rts", True) in self.pins:     # EN released: ROM banner
                    self.lines += [b"ets Jun  8 2016 00:22:57\r\n", b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n",
                                   b"entry 0x400805e4\r\n", b"# connected  ->  http://192.168.1.139\r\n"]
            object.__setattr__(self, k, v)
    ser = Resettable()
    lines = []
    probed = []
    baud, _ = eb.listen("/dev/ttyUSB1", seconds=0.2, out=lines.append, users=lambda p: [],
                        opener=lambda p, b: ser, reset=True, probe_fn=lambda u, out: probed.append(u) or u)
    assert probed == ["http://192.168.1.139"]
    assert baud == 0 and ("rts", True) in ser.pins and ser.pins[-1] == ("rts", False)
    assert any("USB link works" in ln for ln in lines)


def test_undecodable_bytes_are_not_readable():
    lines = []
    garbage = [b"\xff\xfe\x80\n", b"\x8f\x81h\n", b"\x90\xa0qhE\n", b"\xff\xff\n"]

    class G(FakeSerial):
        def __init__(self):
            self.lines = list(garbage)
    baud, _ = eb.listen("/dev/ttyUSB0", seconds=0.1, out=lines.append, users=lambda p: [], opener=lambda p, b: G())
    assert baud == 0 and not any("readable, but" in ln for ln in lines)
    assert sum("unreadable" in ln for ln in lines) == 10


def test_usb_ports_by_board_or_by_socket():
    real = {"/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0": "/dev/ttyUSB1",
            "/dev/serial/by-id/usb-Silicon_Labs_CP2102_A-if00-port0": "/dev/ttyUSB0",
            "/dev/serial/by-id/usb-Silicon_Labs_CP2102_B-if00-port0": "/dev/ttyUSB1"}
    by_path = ["/dev/serial/by-path/platform-xhci-hcd.0-usb-0:1:1.0-port0",
               "/dev/serial/by-path/platform-xhci-hcd.1-usb-0:2:1.0-port0"]

    def finder(by_id):
        return lambda pat: (by_id if "by-id" in pat else by_path if "by-path" in pat
                            else ["/dev/ttyUSB0", "/dev/ttyUSB1"] if "ttyUSB" in pat else [])
    # two different chips (or serial numbers): named by board, whichever socket it is in
    two = list(real)[1:]
    assert eb.serial_ports(finder(two), real=real.get) == sorted(two)
    # two identical CH340s: one by-id name for both boards, so they are named by socket
    assert eb.serial_ports(finder(list(real)[:1]), real=real.get) == by_path
    assert eb.serial_ports(lambda pat: ["/dev/ttyUSB0"] if "ttyUSB" in pat else []) == ["/dev/ttyUSB0"]
