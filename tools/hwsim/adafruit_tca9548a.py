"""Fake adafruit_tca9548a."""


class TCA9548A:
    def __init__(self, i2c, address=0x70):
        self.address = address

    def __getitem__(self, ch):
        return ("tca", ch)
