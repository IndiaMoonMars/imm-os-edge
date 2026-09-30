"""Fake adafruit_tsl2561: one sensor per mux channel, channel 2 absent (tests the retry path)."""
from _signals import wave


class TSL2561:
    def __init__(self, channel):
        self.ch = channel[1]
        if self.ch == 2:
            raise RuntimeError("Failed to find TSL2561! Part 0x0 Rev 0x0")

    @property
    def lux(self):
        return max(0.0, wave([320, 180, 40][self.ch], 25, 300, 2))
