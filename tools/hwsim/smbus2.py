"""Fake smbus2: a MAX17048 fuel gauge (0x36) and a MAX30100 pulse oximeter (0x57) with a finger on it."""
import time
from _signals import ppg, wave


class SMBus:
    def __init__(self, bus=1):
        self.regs = {}
        self._t_read = time.time()

    def close(self):
        pass

    # MAX30100 ------------------------------------------------------------
    def read_byte_data(self, addr, reg):
        if addr == 0x57:
            if reg == 0xFF:
                return 0x11                       # part ID
            if reg == 0x02:                       # FIFO write pointer: samples since last read
                n = min(15, int((time.time() - self._t_read) * 100))
                return n & 0x0F
            if reg in (0x03, 0x04):
                return 0
            return self.regs.get((addr, reg), 0) & ~0x40   # RESET self-clears
        raise OSError(121, "Remote I/O error")

    def write_byte_data(self, addr, reg, value):
        if addr != 0x57:
            raise OSError(121, "Remote I/O error")
        self.regs[(addr, reg)] = value

    def read_i2c_block_data(self, addr, reg, n):
        if addr == 0x36 and reg == 0x04:          # fuel gauge SOC
            soc = wave(84.0, 1.5, 3600)
            return [int(soc), int((soc % 1) * 256)]
        if addr == 0x57 and reg == 0x05:          # FIFO: 4 bytes/sample (IR, RED)
            out, now = [], time.time()
            count = n // 4
            for i in range(count):
                t = self._t_read + (i + 1) / 100.0
                ir = int(ppg(t)); red = int(ppg(t) * 0.62 + 18000)
                out += [ir >> 8 & 0xFF, ir & 0xFF, red >> 8 & 0xFF, red & 0xFF]
            self._t_read = min(now, self._t_read + count / 100.0)
            return out
        raise OSError(121, "Remote I/O error")
