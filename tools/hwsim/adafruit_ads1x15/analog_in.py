from _signals import ecg_volts, wave


class AnalogIn:
    def __init__(self, ads, pin, negative_pin=None):
        self.ads, self.pin = ads, pin

    @property
    def voltage(self):
        if self.ads.address == 0x48 and self.pin == 0:
            return ecg_volts()
        if self.pin == 1:
            return wave(0.04250, 0.00004, 600, 0.000005)      # O2 cell ≈ 42.5 mV in 20.9 % air
        return 0.0
