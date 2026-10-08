"""Both links to a board at once (USB cable + Wi-Fi): every reading published once, none lost."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "sensor_drivers"))
import dual_link  # noqa: E402
import esp32_bridge  # noqa: E402
import external_board_bridge as eb  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_a_reading_with_the_boards_counter_is_published_once_whichever_link_brings_it():
    link = dual_link.DualLink(clock=Clock())
    assert link.accept("usb", 1000) and link.accept("usb", 2000)
    assert not link.accept("wifi", 2000)                 # the Wi-Fi copy of a USB reading
    assert link.accept("wifi", 3000)                     # USB missed this one: Wi-Fi fills it in
    assert not link.accept("usb", 3000)
    assert link.accept("usb", 500)                       # counter went back: the board restarted
    assert not link.accept("wifi", 500)


def test_a_reading_without_a_counter_comes_from_usb_while_usb_delivers():
    clock = Clock()
    link = dual_link.DualLink(clock=clock)
    assert link.accept("wifi")                           # nothing from USB yet: Wi-Fi is used
    assert link.accept("usb")
    clock.t += 1
    assert not link.accept("wifi")                       # USB delivering: the Wi-Fi copy is dropped
    clock.t += dual_link.HOLD_S
    assert link.accept("wifi")                           # USB quiet: Wi-Fi takes over
    assert link.links() == {"usb_link": 1, "wifi_link": 1}
    clock.t += dual_link.LINK_S
    link.accept("wifi")
    assert link.links() == {"usb_link": 0, "wifi_link": 1}


def _line(ms, board=False):
    d = {"ms": ms, "bme280": {"temp": 24.0 + ms / 1e6, "hum": 40.0, "pres": 1010.0}}
    if board:
        d["board"] = {"uptime_s": ms // 1000, "reset_reason": 1, "boot_count": 7, "i2c_err": 0, "bme_resets": 0}
    return d


def test_internal_board_dual_publishes_each_reading_once_with_its_link():
    usb = [_line(1000, board=True), _line(2000), _line(4000)]          # USB missed 3000
    wifi = [_line(1000, board=True), _line(2000), _line(3000), _line(4000)]

    def feeder(lines, via):
        def run(link, _arg):
            for ln in lines:
                link.put(via, ln)
        return run
    sent = []
    esp32_bridge.run_dual("http://imm-sensors.local/json", "/dev/ttyUSB1", lambda p, t: sent.append((t, p)),
                          now=lambda: 5.0, readers=[(feeder(usb, "usb"), None), (feeder(wifi, "wifi"), None)],
                          max_items=len(usb) + len(wifi))
    bme = [p for t, p in sent if p["sensor"] == "bme280"]
    assert sorted(round((p["temp"] - 24) * 1e6) for p in bme) == [1000, 2000, 3000, 4000]   # each once, none lost
    assert {p["via"] for p in bme} <= {"usb", "wifi"}
    board = [p for t, p in sent if p["sensor"] == "board"]
    assert len(board) == 1 and board[0]["usb_link"] in (0, 1) and board[0]["wifi_link"] in (0, 1)
    assert board[0]["usb_link"] or board[0]["wifi_link"]


class FakeSerial:
    def __init__(self, lines, link):
        self.lines, self.link, self.written = [ln.encode() for ln in lines], link, []

    def reset_input_buffer(self):
        pass

    def write(self, b):
        self.written.append(b)

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        self.link.stop.set()
        return b""

    def close(self):
        pass


def test_usb_link_reads_lines_and_tells_the_board_the_pi_reads_its_usb(capsys):
    link = dual_link.DualLink()
    ser = FakeSerial(["# unknown command (STATUS, …)\r\n", json.dumps(_line(1000)) + "\r\n",
                      "# unknown command (STATUS, …)\r\n", "# bme280: re-initialising\r\n"], link)
    esp32_bridge.usb_link(link, "/dev/ttyUSB1", opener=lambda p: ser)
    assert ser.written[0] == b"USB_HOST\n"
    via, line = link.queue.get_nowait()
    assert via == "usb" and line["ms"] == 1000
    err = capsys.readouterr().err
    assert err.count("predates USB_HOST") == 1                 # old firmware: said once, not every 20 s
    assert "bme280: re-initialising" in err


def test_external_board_dual_dedups_by_its_uptime():
    data = {"uptime": 41, "rad": {"cpm": 18.0, "uSvh": 0.117}, "gnss": {"sats": 0, "fix": False}}
    usb_line = eb.recognise(data)                       # what its sketch would print on USB, recognised
    sent = []

    def feeder(lines, via):
        def run(link, _arg):
            for ln in lines:
                link.put(via, ln)
        return run
    eb.run_dual("http://192.168.1.125/data", "/dev/ttyUSB0", lambda p, t: sent.append((t, p)), "exterior",
                now=lambda: 5.0, readers=[(feeder([usb_line], "usb"), None), (feeder([eb.recognise(data)], "wifi"), None)],
                max_items=2)
    geiger = [p for t, p in sent if p["sensor"] == "geiger"]
    assert len(geiger) == 1 and geiger[0]["cpm"] == 18.0
    board = [p for t, p in sent if p["sensor"] == "board"]
    assert board and "usb_link" in board[0] and "wifi_link" in board[0]


def test_both_links_used_only_when_the_port_is_pinned(monkeypatch):
    calls = []
    monkeypatch.setattr(esp32_bridge, "run_dual", lambda *a, **k: calls.append("dual"))
    monkeypatch.setattr(esp32_bridge, "run_http", lambda *a, **k: calls.append("http"))
    monkeypatch.setattr(esp32_bridge, "make_publisher", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["esp32_bridge.py"])
    monkeypatch.setenv("ESP32_URL", "http://imm-sensors.local/json")
    monkeypatch.delenv("ESP32_PORT", raising=False)
    esp32_bridge.main()
    monkeypatch.setenv("ESP32_PORT", "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0")
    esp32_bridge.main()
    assert calls == ["http", "dual"]
