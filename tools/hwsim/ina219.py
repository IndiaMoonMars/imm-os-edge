"""Fake pi-ina219 (bus INA219 at 0x40, solar INA219 at 0x44)."""
from _signals import wave


class DeviceRangeError(Exception):
    pass


class INA219:
    def __init__(self, shunt_ohms, max_expected_amps=None, busnum=None, address=0x40, **kw):
        if busnum is None:
            raise RuntimeError("Could not determine default I2C bus for platform.")   # like the real library
        self.address = address

    def configure(self, *a, **kw):
        pass

    def voltage(self):
        return wave(12.18, 0.05, 300, 0.005)

    def current(self):
        return wave(840, 60, 120, 5)

    def power(self):
        if self.address == 0x44:
            return max(0.0, wave(9000, 3000, 900, 50))            # mW of solar
        return self.voltage() * self.current()
