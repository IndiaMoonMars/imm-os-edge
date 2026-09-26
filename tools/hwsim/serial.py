"""Fake pyserial.

- The header UART (MQ7_PORT, /dev/ttyAMA0…): the STM32 MQ-7 controller, one heater cycle
  every HWSIM_MQ7_PERIOD seconds.
- A USB port (ESP32_PORT, …USB…/…ACM…/…esp32…): the ESP32 sensor board, one JSON line per second.
"""
import json
import os
import time
from _signals import wave


def _is_esp32(port):
    return any(s in str(port or "") for s in ("USB", "ACM", "esp32", "by-id"))


class Serial:
    def __init__(self, port=None, baudrate=9600, timeout=None, **kw):
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.dtr = self.rts = True
        if port is not None:
            self.open()

    def open(self):
        self.timeout = self.timeout or 1
        self.esp32 = _is_esp32(self.port)
        self.period = 1.0 if self.esp32 else float(os.getenv("HWSIM_MQ7_PERIOD", "10"))
        self._t0 = time.time()
        self._next = time.time() + self.period
        self._pending = [b"# IMM-OS ESP32 sensor board started\n" if self.esp32 else
                         b"# IMM-OS MQ-7 controller started (60 s @ 5 V, 90 s @ 1.4 V)\n"]

    def reset_input_buffer(self):
        pass

    def write(self, data):
        cmd = data.strip().upper()
        if cmd == b"CAL":
            self._pending.append(b"# CAL: R0 is set at the end of this cycle (keep the sensor in clean air)\n")
        elif cmd == b"STATUS":
            self._pending.append(b"# sensors: bme280=0x76 scd40=0x62 bno055=0x28 o2=found mq4_r0=1.200 divider=2.00\n"
                                 if self.esp32 else b"# phase=5.0V elapsed_s=12 r0=1000 cycles=3\n")
        elif cmd in (b"CAL_MQ4", b"CAL_O2"):
            self._pending.append(b"# " + cmd + b": ok (hwsim)\n")
        return len(data)

    def readline(self):
        if self._pending:
            return self._pending.pop(0)
        wait = self._next - time.time()
        if wait > self.timeout:
            time.sleep(self.timeout)
            return b""
        time.sleep(max(0, wait))
        self._next += self.period
        if self.esp32:
            return (json.dumps(self._esp32_line(), separators=(",", ":")) + "\n").encode()
        ppm = max(0.0, wave(3.0, 1.5, 900, 0.3))
        self._pending.append(f"CO:{ppm:.1f}\n".encode())
        return f"# vout=1.331 rs=27630 r0=1000 ratio={27.5 * (3.0 / max(ppm, 0.1)) ** 0.1:.2f}\n".encode()

    def _esp32_line(self):
        ms = int((time.time() - self._t0) * 1000)
        ch4 = max(0.5, wave(4.0, 2.0, 600, 0.3))
        line = {"ms": ms,
                "bme280": {"temp": round(wave(22.4, 0.4, 900, 0.03), 2), "hum": round(wave(44, 2, 1200, 0.1), 2),
                           "pres": round(wave(1009.5, 0.6, 3600, 0.05), 2)},
                "bno055": {"heading_deg": round(wave(182, 1.5, 300, 0.2) % 360, 2), "roll_deg": round(wave(0.4, 0.3, 60, 0.05), 2),
                           "pitch_deg": round(wave(-1.2, 0.3, 75, 0.05), 2), "lin_acc_ms2": round(abs(wave(0.02, 0.02, 10, 0.01)), 2),
                           "imu_calib": 3},
                "o2": {"o2_pct": round(wave(20.9, 0.08, 1800, 0.02), 2)},
                "mq4": {"vout_mv": round(930 * (ch4 / 4.0) ** 0.36), "rs_r0": round((ch4 / 1012.7) ** (1 / -2.786), 3),
                        "ch4_ppm": round(ch4, 1), "warming": 0, "calibrated": 1}}
        if ms // 1000 % 5 == 4:
            line["scd40"] = {"co2_ppm": round(wave(640, 40, 1800, 3)), "temp": round(wave(23.1, 0.4, 900, 0.03), 2),
                             "hum": round(wave(42, 2, 1200, 0.1), 2)}
        return line

    def close(self):
        pass
