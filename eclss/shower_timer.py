#!/usr/bin/env python3
"""
Shower timer — PIR motion sensor (HC-SR501) in the hygiene module.

A session starts on the first motion and ends after SHOWER_IDLE_S seconds with no
motion (set the HC-SR501's own hold-time pot to minimum). Sessions shorter than
SHOWER_MIN_S (someone passing the door) are ignored. Water use is estimated from the
duration at SHOWER_LPM litres/minute (a low-flow head is ~6–9 L/min).

Environment:
  PIR_GPIO=27  SHOWER_IDLE_S=90  SHOWER_MIN_S=30  SHOWER_LPM=9.0
  ECLSS_API_URL=http://imm.local/eclss

  --simulate   one 3-minute shower, then exit (the old behaviour)
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from hw import EventPoster, digital_input, env_float, env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [shower_timer] %(message)s")
log = logging.getLogger(__name__)

ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/water/shower"


class ShowerSession:
    """Motion samples in, finished sessions out (pure logic, testable)."""

    def __init__(self, idle_s: float, min_s: float):
        self.idle_s, self.min_s = idle_s, min_s
        self.started = None
        self.last_motion = None

    def update(self, motion: bool, now: float):
        """Returns the finished session's duration in seconds, or None."""
        if motion:
            if self.started is None:
                self.started = now
                log.info("Shower session started")
            self.last_motion = now
            return None
        if self.started is not None and now - self.last_motion >= self.idle_s:
            duration = self.last_motion - self.started
            self.started = self.last_motion = None
            if duration >= self.min_s:
                return round(duration, 1)
            log.info("Ignored %.0f s of motion (shorter than a shower)", duration)
        return None


def report(poster: EventPoster, duration: float, lpm: float) -> None:
    liters = round(duration / 60.0 * lpm, 1)
    log.info("Shower: %.1f min, ~%.1f L", duration / 60.0, liters)
    poster.post({"duration_seconds": duration, "estimated_liters": liters})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    poster = EventPoster(API_URL, "shower")
    lpm = env_float("SHOWER_LPM", 9.0)

    if simulate_requested(args.simulate):
        report(poster, 180.0, lpm)
        return

    pin = env_int("PIR_GPIO", 27)
    pir = digital_input(pin, pull_up=False)   # HC-SR501 drives the line high on motion
    session = ShowerSession(env_float("SHOWER_IDLE_S", 90), env_float("SHOWER_MIN_S", 30))
    log.info("PIR on GPIO%d", pin)
    while True:
        duration = session.update(bool(pir.value), time.monotonic())
        if duration is not None:
            report(poster, duration, lpm)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
