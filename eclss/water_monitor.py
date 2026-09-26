#!/usr/bin/env python3
"""
Water flow monitor — YF-S201 hall-effect flow meter on a water line.

The meter gives ~450 pulses per litre on its signal wire (5 V logic: use a voltage
divider or level shifter into the Pi's 3.3 V GPIO). A draw event starts with the
first pulse and ends after FLOW_IDLE_S seconds without flow; each event is sent to
the ECLSS API with the running daily total (reset at local midnight).

Environment (see systemd/edge.env.example):
  FLOW_GPIO=17  FLOW_PULSES_PER_L=450  FLOW_IDLE_S=5  FLOW_MIN_ML=20
  FLOW_SOURCE=drinking_line   ECLSS_API_URL=http://imm.local/eclss

  --simulate   a 500 mL draw every minute (no hardware)
"""
import argparse
import logging
import os
import sys
import threading
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from hw import EventPoster, digital_input, env_float, env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [water_monitor] %(message)s")
log = logging.getLogger(__name__)

ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/water/log"


class FlowEventTracker:
    """Turns a growing pulse count into discrete draw events (pure logic, testable)."""

    def __init__(self, pulses_per_l: float, idle_s: float, min_ml: float):
        self.ml_per_pulse = 1000.0 / pulses_per_l
        self.idle_s, self.min_ml = idle_s, min_ml
        self._last_count = 0
        self._event_pulses = 0
        self._last_flow_at = None

    def update(self, count: int, now: float):
        """Feed the cumulative pulse count; returns a finished event's mL, or None."""
        new = count - self._last_count
        self._last_count = count
        if new > 0:
            self._event_pulses += new
            self._last_flow_at = now
            return None
        if self._event_pulses and self._last_flow_at is not None and now - self._last_flow_at >= self.idle_s:
            ml = self._event_pulses * self.ml_per_pulse
            self._event_pulses, self._last_flow_at = 0, None
            return round(ml, 1) if ml >= self.min_ml else None
        return None


class DailyTotal:
    def __init__(self):
        self.day, self.ml = date.today(), 0.0

    def add(self, ml: float) -> float:
        if date.today() != self.day:
            self.day, self.ml = date.today(), 0.0
        self.ml += ml
        return round(self.ml, 1)


def run_hardware(poster: EventPoster, source: str) -> None:
    pin = env_int("FLOW_GPIO", 17)
    sensor = digital_input(pin, pull_up=True)
    count = [0]
    lock = threading.Lock()

    def pulse():
        with lock:
            count[0] += 1

    sensor.when_activated = pulse
    tracker = FlowEventTracker(env_float("FLOW_PULSES_PER_L", 450), env_float("FLOW_IDLE_S", 5),
                               env_float("FLOW_MIN_ML", 20))
    daily = DailyTotal()
    log.info("YF-S201 on GPIO%d (%s)", pin, source)
    while True:
        with lock:
            c = count[0]
        ml = tracker.update(c, time.monotonic())
        if ml is not None:
            total = daily.add(ml)
            log.info("Draw of %.0f mL on %s (today %.0f mL)", ml, source, total)
            poster.post({"event_ml": ml, "daily_total_ml": total, "source": source})
        time.sleep(0.5)


def run_simulated(poster: EventPoster, source: str) -> None:
    daily = DailyTotal()
    log.info("Simulating a 500 mL draw every minute")
    while True:
        total = daily.add(500.0)
        log.info("Draw of 500 mL (simulated, today %.0f mL)", total)
        poster.post({"event_ml": 500.0, "daily_total_ml": total, "source": source})
        time.sleep(60)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    source = os.getenv("FLOW_SOURCE", "drinking_line")
    poster = EventPoster(API_URL, f"water_{source}")
    (run_simulated if simulate_requested(args.simulate) else run_hardware)(poster, source)


if __name__ == "__main__":
    main()
