#!/usr/bin/env python3
import time
import argparse
import logging
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s [lighting] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCK (PCA9685 I2C)
class PCA9685_Mock:
    def set_pwm(self, channel, on, off):
        log.debug(f"I2C Cmd: CH{channel} ON={on} OFF={off}")

pwm = PCA9685_Mock()

def set_lighting(zone: str, brightness: int, kelvin: int):
    # WS2812B logical translation based on color temperature (Kelvin)
    # 2000K (Warm) -> 6500K (Daylight)
    brightness = max(0, min(100, brightness))
    kelvin = max(2000, min(6500, kelvin))
    
    # Send simulated I2C commands
    log.info(f"Targeting {zone} -> Brightness: {brightness}%, Color Temp: {kelvin}K")
    pwm.set_pwm(0, 0, int(40.95 * brightness))
    
def circadian_loop():
    log.info("Starting Auto-Circadian Loop (12 Hour compression)")
    # Simulation: compress 12 hours into 60 seconds
    schedule = [
        (2500, "06:00 - Dawn"),
        (3500, "08:00 - Morning"),
        (5500, "12:00 - Midday"),
        (4000, "16:00 - Afternoon"),
        (2000, "20:00 - Evening")
    ]
    for temp, phase in schedule:
        log.info(f"Circadian Phase: {phase}")
        set_lighting("all_zones", 80, temp)
        time.sleep(12)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", type=str, default="core")
    parser.add_argument("--brightness", type=int, default=80)
    parser.add_argument("--kelvin", type=int, default=5000)
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    if args.auto:
        circadian_loop()
    else:
        set_lighting(args.zone, args.brightness, args.kelvin)
