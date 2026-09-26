"""MQ-7: STM32 firmware (simulated on the PC) and the Pi-side UART bridge."""
import os
import shutil
import subprocess

import pytest

import bringup
import mq7_uart_bridge as bridge

FW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmware", "stm32-mq7")


def adc_for(rs_ohms, rl=10000.0, vc=5.0, top=100000.0, bottom=200000.0, vref=3.3):
    load = rl * (top + bottom) / (rl + top + bottom)          # RL in parallel with the divider
    vout = vc * load / (load + rs_ohms)
    divider = (top + bottom) / bottom
    return round(vout / divider / vref * 4095)


@pytest.fixture(scope="module")
def firmware_sim(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("g++ not available")
    exe = str(tmp_path_factory.mktemp("fw") / "mq7sim")
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(FW, "test", "stub"),
                    "-o", exe, os.path.join(FW, "test", "sim.cpp")], check=True)

    def run(script: str, tmp_path) -> list:
        f = tmp_path / "script.txt"
        f.write_text(script)
        return subprocess.run([exe, str(f)], capture_output=True, text=True, check=True).stdout.splitlines()
    return run


def test_firmware_heater_cycle_calibration_and_ppm(firmware_sim, tmp_path):
    out = firmware_sim(f"""heater
adc {adc_for(27500)}
send cal
run 59
heater
run 2
heater
run 90
adc {adc_for(1000)}
run 150
reset
send STATUS
run 1
""", tmp_path)
    heater = [int(l.split()[1]) for l in out if l.startswith("HEATER")]
    assert heater[0] == 4095 and heater[1] == 4095              # 5 V phase lasts 60 s
    assert heater[2] == round(0.28 ** 2 * 4095)                 # 1.4 V power-equivalent PWM (7.8 %)
    assert any(l in ("# R0=1000", "# R0=999", "# R0=1001") for l in out)   # clean air: Rs/R0 = 27.5
    co = [float(l[3:]) for l in out if l.startswith("CO:")]
    assert co[0] == pytest.approx(99.042 * 27.5 ** -1.518, abs=0.1)
    assert co[1] == pytest.approx(99.0, abs=0.5)                 # Rs = R0 → ~99 ppm
    assert any(" r0=1000" in l or " r0=999" in l or " r0=1001" in l for l in out if l.startswith("# phase="))   # R0 kept in flash


def test_firmware_uncalibrated_sends_no_co(firmware_sim, tmp_path):
    out = firmware_sim(f"adc {adc_for(20000)}\nrun 151\n", tmp_path)
    assert not any(l.startswith("CO:") for l in out)
    assert any("not calibrated" in l for l in out)


def test_bridge_parses_firmware_lines():
    assert bridge.parse_line("CO:12.4\r\n") == ("co", 12.4)
    assert bridge.parse_line("# vout=1.333 rs=27501")[0] == "info"
    assert bridge.parse_line("garbage 12")[0] == "bad"
    assert bridge.parse_line("   ") is None


class FakeSerial:
    def __init__(self, lines):
        self.lines = [l.encode() for l in lines]

    def reset_input_buffer(self):
        pass

    def readline(self):
        if not self.lines:
            raise KeyboardInterrupt           # end of the test stream
        return self.lines.pop(0)


def test_bridge_publishes_only_co_lines(capsys):
    published = []
    with pytest.raises(KeyboardInterrupt):
        bridge.read_loop(FakeSerial(["# IMM-OS MQ-7 controller started\n", "CO:3.5\n", "bogus\n", "CO:4\n"]),
                         published.append)
    assert [p["co_ppm"] for p in published] == [3.5, 4.0]
    err = capsys.readouterr().err
    assert '"info": "stm32: IMM-OS MQ-7 controller started"' in err and "Invalid STM32 data" in err


def test_bringup_keeps_info_lines_out_of_errors(tmp_path):
    import json
    import sys
    script = tmp_path / "drv.py"
    script.write_text("import json, sys\n"
                      f"print(json.dumps({{'info': 'stm32: not calibrated'}}), file=sys.stderr)\n"
                      "print(json.dumps({'sensor': 'mq7', 'co_ppm': 1.0}), flush=True)\n")
    notes = []
    readings, errors = bringup.run_driver([sys.executable, str(script)], dict(os.environ), 1, 10, notes)
    assert readings == [{"sensor": "mq7", "co_ppm": 1.0}] and errors == []
    assert notes == ["stm32: not calibrated"]


class IdBus:
    def __init__(self, value):
        self.value = value

    def read_byte_data(self, addr, reg):
        assert (addr, reg) == (0x76, 0xD0)
        return self.value


def test_bringup_identifies_bmp280_impostor():
    names = bringup.sensors()["bme280"].chip_id[2]
    assert bringup.identify(IdBus(0x60), 0x76, 0xD0, names) == (0x60, "BME280")
    value, part = bringup.identify(IdBus(0x58), 0x76, 0xD0, names)
    assert "BMP280" in part


def test_ecg_and_o2_default_to_separate_adcs(monkeypatch):
    monkeypatch.delenv("O2_ADS_ADDRESS", raising=False)
    table = bringup.sensors()
    assert table["ecg"].i2c == [0x48] and table["o2"].i2c == [0x49]
