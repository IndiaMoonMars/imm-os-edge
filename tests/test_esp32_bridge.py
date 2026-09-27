"""sensor_drivers/esp32_bridge.py and ESP32 port detection (no hardware needed)."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sensor_drivers"))

import bringup  # noqa: E402
import esp32_bridge  # noqa: E402
import hw  # noqa: E402

LINE = ('{"ms":7065,"bme280":{"temp":25.08,"hum":55.0,"pres":1006.53},'
        '"scd40":{"co2_ppm":612,"temp":24.43,"hum":40.0},'
        '"bno055":{"heading_deg":90.0,"roll_deg":-5.0,"pitch_deg":2.0,"lin_acc_ms2":0.5,"imu_calib":3,'
        '"grav_ms2":9.8,"mag_ut":40.7,"gyro_dps":2.29,"temp":27,"calib_gyro":3,"calib_acc":2,"calib_mag":1},'
        '"o2":{"o2_pct":20.87},'
        '"mq4":{"vout_mv":930,"rs_rl":4.376,"rs_r0":1.03,"ch4_ppm":4.1,"warming":0,"calibrated":1}}')


def test_parse_line():
    assert esp32_bridge.parse_line("") is None
    assert esp32_bridge.parse_line("# sensors: bme280=0x76\n") == ("info", "sensors: bme280=0x76")
    assert esp32_bridge.parse_line("\x00\xffgarbage")[0] == "bad"
    assert esp32_bridge.parse_line("[1, 2]")[0] == "bad"
    kind, data = esp32_bridge.parse_line(LINE)
    assert kind == "data" and data["ms"] == 7065


def test_one_line_becomes_one_payload_per_sensor_in_the_pi_drivers_shapes():
    out = dict((p["sensor"], (t, p)) for t, p in esp32_bridge.to_payloads(json.loads(LINE), 1790000000.1234))
    assert set(out) == {"bme280", "scd40", "bno055", "o2", "mq4"}
    topic, bme = out["bme280"]
    assert topic == "habitat/sensors/bme280/zone1"
    dp = bme.pop("dew_point_c")
    assert bme == {"sensor": "bme280", "timestamp": 1790000000.123, "temp": 25.08, "hum": 55.0, "pres": 1006.53}
    assert dp == pytest.approx(15.4, abs=0.1)                  # psychrometric table: 25 °C / 55 % → 15.3-15.4 °C
    assert out["scd40"][1]["dew_point_c"] == pytest.approx(10.0, abs=0.1)    # table: 24 °C/40 % 9.6, 25 °C/40 % 10.5
    assert out["o2"][1]["o2_pct"] == 20.87
    bno = out["bno055"][1]
    assert (bno["grav_ms2"], bno["mag_ut"], bno["gyro_dps"], bno["temp"]) == (9.8, 40.7, 2.29, 27.0)
    assert all(isinstance(bno[k], int) for k in ("imu_calib", "calib_gyro", "calib_acc", "calib_mag"))
    assert (bno["calib_gyro"], bno["calib_acc"], bno["calib_mag"]) == (3, 2, 1)
    assert out["mq4"][1] == {"sensor": "mq4", "timestamp": 1790000000.123, "vout_mv": 930.0, "rs_rl": 4.376,
                             "rs_r0": 1.03, "ch4_ppm": 4.1, "warming": 0, "calibrated": 1}


def test_dew_point():
    assert esp32_bridge.dew_point(20.0, 100.0) == pytest.approx(20.0, abs=0.01)   # saturated: dew point = temp
    assert esp32_bridge.dew_point(30.0, 50.0) == pytest.approx(18.4, abs=0.15)
    assert esp32_bridge.dew_point(20.0, 0.0) is None


def test_partial_and_malformed_sections():
    data = {"ms": 1, "bme280": {"temp": "hot", "hum": True, "pres": 1000}, "mq4": {"vout_mv": 900, "warming": 1},
            "scd40": [], "future": {"x": 1}}
    out = {p["sensor"]: p for _, p in esp32_bridge.to_payloads(data, 1.0)}
    assert out["bme280"] == {"sensor": "bme280", "timestamp": 1.0, "pres": 1000.0}     # strings/bools dropped
    assert out["mq4"] == {"sensor": "mq4", "timestamp": 1.0, "vout_mv": 900.0, "warming": 1}   # no ppm yet
    assert "scd40" not in out and "future" not in out


class FakeSerial:
    def __init__(self, lines):
        self.lines = [line.encode() for line in lines]

    def reset_input_buffer(self):
        pass

    def readline(self):
        if not self.lines:
            raise KeyboardInterrupt
        return self.lines.pop(0)


def test_read_loop_publishes_and_explains_mq4_state(capsys):
    published = []
    ser = FakeSerial(["# IMM-OS ESP32 sensor board started", '{"ms":1,"mq4":{"vout_mv":930,"warming":1,"calibrated":0}}',
                      '{"ms":2,"mq4":{"vout_mv":930,"warming":1,"calibrated":0}}', "noise"])
    with pytest.raises(KeyboardInterrupt):
        esp32_bridge.read_loop(ser, lambda p, t: published.append((t, p)), now=lambda: 5.0)
    assert [t for t, _ in published] == ["habitat/sensors/mq4/zone1"] * 2
    err = capsys.readouterr().err
    assert err.count("MQ-4 warming up") == 1                                     # said once, not every second
    assert "esp32: IMM-OS ESP32 sensor board started" in err and "Invalid ESP32 line" in err


def test_esp32_port_detection():
    by_id = {"/dev/serial/by-id/*": ["/dev/serial/by-id/usb-FTDI_x-if00",
                                     "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_0001-if00-port0"]}
    assert hw.esp32_port({}, lambda p: by_id.get(p, [])).endswith("CP2102_USB_to_UART_Bridge_0001-if00-port0")
    assert hw.esp32_port({}, lambda p: ["/dev/ttyUSB0"] if p == "/dev/ttyUSB0" else []) == "/dev/ttyUSB0"
    assert hw.esp32_port({}, lambda p: []) == ""
    assert hw.esp32_port({"ESP32_PORT": "/dev/ttyX"}, lambda p: []) == "/dev/ttyX"


def test_bringup_treats_esp32_as_usb_board(monkeypatch):
    table = bringup.sensors()
    esp = table["esp32"]
    assert esp.usb and esp.replaces == ["bme280", "scd40", "o2"]
    monkeypatch.setattr(bringup, "esp32_port", lambda: "")
    assert not bringup.connected(esp, set(), True)
    monkeypatch.setattr(bringup, "esp32_port", lambda: "/dev/ttyUSB0")
    assert bringup.connected(esp, set(), False)
