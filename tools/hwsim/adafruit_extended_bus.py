"""Fake adafruit_extended_bus."""


class ExtendedI2C:
    def __init__(self, bus_id, frequency=400000):
        self.bus_id = bus_id
