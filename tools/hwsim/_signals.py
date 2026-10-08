"""Plausible, slowly varying signals shared by the fake hardware modules."""
import math
import os
import random
import time

T0 = time.time()
rnd = random.Random(int(os.getenv("HWSIM_SEED", "7")))


def wave(base, amp, period_s, noise=0.0):
    t = time.time() - T0
    return base + amp * math.sin(2 * math.pi * t / period_s) + rnd.gauss(0, noise)


def ecg_volts(t=None):
    """AD8232-like output around 1.65 V: P wave, QRS spike, T wave at 72 bpm."""
    t = (time.time() if t is None else t) % (60 / 72)
    ph = t / (60 / 72)
    v = 1.65
    v += 0.08 * math.exp(-((ph - 0.15) / 0.03) ** 2)       # P
    v -= 0.10 * math.exp(-((ph - 0.28) / 0.008) ** 2)      # Q
    v += 0.95 * math.exp(-((ph - 0.30) / 0.010) ** 2)      # R
    v -= 0.20 * math.exp(-((ph - 0.32) / 0.010) ** 2)      # S
    v += 0.22 * math.exp(-((ph - 0.55) / 0.06) ** 2)       # T
    return v + rnd.gauss(0, 0.004)


def ppg(t, bpm=68.0):
    ph = (t * bpm / 60) % 1.0
    pulse = -900 * math.exp(-((ph - 0.15) / 0.06) ** 2) - 250 * math.exp(-((ph - 0.45) / 0.05) ** 2)
    return 50000 + pulse + 300 * math.sin(2 * math.pi * 0.25 * t)
