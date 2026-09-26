"""Fake pyserial: the STM32 MQ-7 controller's output, one cycle every HWSIM_MQ7_PERIOD seconds."""
import os
import time
from _signals import wave


class Serial:
    def __init__(self, port=None, baudrate=9600, timeout=None, **kw):
        self.period = float(os.getenv("HWSIM_MQ7_PERIOD", "10"))
        self.timeout = timeout or 1
        self._next = time.time() + self.period
        self._pending = [b"# IMM-OS MQ-7 controller started (60 s @ 5 V, 90 s @ 1.4 V)\n"]

    def reset_input_buffer(self):
        pass

    def write(self, data):
        if data.strip().upper() == b"CAL":
            self._pending.append(b"# CAL: R0 is set at the end of this cycle (keep the sensor in clean air)\n")
        elif data.strip().upper() == b"STATUS":
            self._pending.append(b"# phase=5.0V elapsed_s=12 r0=1000 cycles=3\n")
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
        ppm = max(0.0, wave(3.0, 1.5, 900, 0.3))
        self._pending.append(f"CO:{ppm:.1f}\n".encode())
        return f"# vout=1.331 rs=27630 r0=1000 ratio={27.5 * (3.0 / max(ppm, 0.1)) ** 0.1:.2f}\n".encode()

    def close(self):
        pass
