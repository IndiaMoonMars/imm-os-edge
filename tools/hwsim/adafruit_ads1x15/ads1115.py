"""Fake ADS1115: 0x48 carries an AD8232 ECG on A0, 0x49 a galvanic O2 cell on A1."""
from .ads1x15 import Mode

P0, P1, P2, P3 = 0, 1, 2, 3


class ADS1115:
    def __init__(self, i2c, gain=1, data_rate=None, mode=Mode.SINGLE, address=0x48):
        self.address, self.gain, self.data_rate, self.mode = address, gain, data_rate, mode
