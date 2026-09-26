"""Fake adafruit_scd4x: a reading every 5 s like the real sensor."""
import time
from _signals import wave


class SCD4X:
    def __init__(self, i2c, address=0x62):
        self._next = time.time() + 5

    def start_periodic_measurement(self):
        self._next = time.time() + 5

    @property
    def data_ready(self):
        if time.time() >= self._next:
            self._next += 5
            return True
        return False

    @property
    def CO2(self):  # noqa: N802 (library name)
        return int(wave(640, 80, 1200, 4))

    @property
    def temperature(self):
        return wave(22.9, 0.6, 600, 0.03)

    @property
    def relative_humidity(self):
        return wave(45.0, 3.0, 900, 0.1)
