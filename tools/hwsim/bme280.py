"""Fake RPi.bme280."""
from types import SimpleNamespace
from _signals import wave


def load_calibration_params(bus, address):
    return object()


def sample(bus, address, params):
    return SimpleNamespace(temperature=wave(22.4, 0.6, 600, 0.02), humidity=wave(46.0, 3.0, 900, 0.1),
                           pressure=wave(1009.8, 0.8, 1800, 0.03))
