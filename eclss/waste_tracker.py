#!/usr/bin/env python3
"""
Waste tracker — HX711 load cell under the waste bin + RFID reader for the waste type.

The crew scans the waste category tag (e.g. a card labelled FOOD / PLASTIC / HYGIENE)
and drops the waste in. When the bin weight rises and settles, the deposit (weight
increase) is sent to the ECLSS API with the last tag scanned within WASTE_TAG_WINDOW_S
(or "untagged"). Emptying the bin (weight drop) just resets the baseline.

Environment:
  HX711_DOUT=5  HX711_SCK=6  HX711_SCALE=<counts per kg, from --calibrate>
  WASTE_MIN_KG=0.02  WASTE_TAG_WINDOW_S=120  WASTE_CONTAINER=main_bin
  WASTE_RFID_DEVICE=/dev/input/by-id/...  or  WASTE_RFID_BACKEND=rc522
  ECLSS_API_URL=http://imm.local/eclss

  --calibrate KG   tare the empty bin, then place KG on it; prints HX711_SCALE
  --simulate       old behaviour: a 1 kg food-waste deposit every 30 s
"""
import argparse
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from hw import EventPoster, env_float, env_int, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [waste_tracker] %(message)s")
log = logging.getLogger(__name__)

ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/waste/log"


class DepositDetector:
    """
    Weight samples in, deposits out (pure logic, testable). A change counts once
    `settle` consecutive samples agree within `tolerance` kg.
    """

    def __init__(self, min_kg: float, tolerance: float = 0.01, settle: int = 3):
        self.min_kg, self.tol, self.settle = min_kg, tolerance, settle
        self.baseline = None
        self._recent = []

    def update(self, weight: float):
        """Returns the deposited kg when a settled increase is seen, else None."""
        self._recent = (self._recent + [weight])[-self.settle:]
        if len(self._recent) < self.settle or max(self._recent) - min(self._recent) > self.tol:
            return None                       # still moving (lid open, bag dropping)
        settled = sum(self._recent) / len(self._recent)
        if self.baseline is None:
            self.baseline = settled
            return None
        delta = settled - self.baseline
        if delta >= self.min_kg:
            self.baseline = settled
            return round(delta, 3)
        if delta <= -self.min_kg:            # bin emptied
            log.info("Bin emptied (%.2f kg removed); new baseline", -delta)
            self.baseline = settled
        return None


class LastTag:
    def __init__(self, window_s: float):
        self.window_s = window_s
        self._tag, self._at = None, 0.0
        self._lock = threading.Lock()

    def set(self, tag: str) -> None:
        with self._lock:
            self._tag, self._at = tag, time.monotonic()
        log.info("Waste tag scanned: %s", tag)

    def take(self) -> str:
        with self._lock:
            fresh = self._tag and time.monotonic() - self._at <= self.window_s
            tag = self._tag if fresh else "untagged"
            self._tag = None
            return tag


def open_scale():
    from hx711 import HX711
    hx = HX711(env_int("HX711_DOUT", 5), env_int("HX711_SCK", 6))
    hx.scale = env_float("HX711_SCALE", 0) or 1.0
    return hx


def calibrate(known_kg: float) -> None:
    hx = open_scale()
    input("Empty the bin completely, then press Enter to tare… ")
    hx.tare()
    input(f"Place exactly {known_kg} kg on the bin, then press Enter… ")
    raw = hx.read_median(25) - hx.offset
    print(f"HX711_SCALE={raw / known_kg:.1f}   (add to /etc/imm-os/edge.env)")
    hx.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--calibrate", type=float, metavar="KG")
    args = p.parse_args()
    if args.calibrate:
        return calibrate(args.calibrate)

    container = os.getenv("WASTE_CONTAINER", "main_bin")
    poster = EventPoster(API_URL, f"waste_{container}")

    if simulate_requested(args.simulate):
        while True:
            log.info("Deposit 1.0 kg | TAG_FOOD_WASTE_01 (simulated)")
            poster.post({"weight_kg": 1.0, "rfid_tag": "TAG_FOOD_WASTE_01", "container": container})
            time.sleep(30)

    if not os.getenv("HX711_SCALE"):
        raise SystemExit("HX711_SCALE not set: run  waste_tracker.py --calibrate 1.0  first")
    hx = open_scale()
    hx.tare()
    log.info("Scale tared (offset %.0f); watching %s", hx.offset, container)

    last_tag = LastTag(env_float("WASTE_TAG_WINDOW_S", 120))
    try:
        from tag_readers import open_tag_reader
        reader = open_tag_reader("WASTE_RFID")

        def read_tags():
            for tag in reader:
                last_tag.set(tag)
        threading.Thread(target=read_tags, daemon=True).start()
    except SystemExit as exc:
        log.warning("No RFID reader configured (%s); deposits will be 'untagged'", exc)

    detector = DepositDetector(env_float("WASTE_MIN_KG", 0.02))
    while True:
        try:
            kg = detector.update(hx.weight())
        except TimeoutError as exc:
            log.error("Scale read failed: %s", exc)
            time.sleep(5)
            continue
        if kg is not None:
            tag = last_tag.take()
            log.info("Deposit %.3f kg | %s", kg, tag)
            poster.post({"weight_kg": kg, "rfid_tag": tag, "container": container})
        time.sleep(1.0)


if __name__ == "__main__":
    main()
