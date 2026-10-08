"""Fake smbus2: a MAX17048 fuel gauge (0x36), a MAX30100 pulse oximeter (0x57) with a finger
on it, and two INA219s: the 12 V habitat bus (0x40) and the solar input (0x44), 0.1 Ω shunts."""
import time
from _signals import ppg, wave


INA219_ADDRESSES = (0x40, 0x44)


def ina219_reading(addr):
    """(bus volts, mA) for the habitat bus (0x40) or the solar input (0x44)."""
    if addr == 0x44:
        return wave(18.4, 0.3, 900, 0.02), max(0.0, wave(490, 160, 900, 5))
    return wave(12.18, 0.05, 300, 0.005), wave(840, 60, 120, 5)


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

    def write_i2c_block_data(self, addr, reg, data):
        if addr not in INA219_ADDRESSES:
            raise OSError(121, "Remote I/O error")
        self.regs[(addr, reg)] = data[0] << 8 | data[1]

    def write_byte_data(self, addr, reg, value):
        if addr != 0x57:
            raise OSError(121, "Remote I/O error")
        self.regs[(addr, reg)] = value

    def read_i2c_block_data(self, addr, reg, n):
        if addr in INA219_ADDRESSES and reg in (0x01, 0x02):
            volts, ma = ina219_reading(addr)
            raw = (int(volts / 0.004) << 3 | 0x2) if reg == 0x02 else int(round(ma * 0.1 / 0.01)) & 0xFFFF
            return [raw >> 8 & 0xFF, raw & 0xFF]
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
