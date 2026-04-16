#!/usr/bin/env python3
import time
import logging
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s [eclss_pid] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (Relays)
class RelayBoard:
    def set_hvac(self, state):
        log.info(f"HVAC Switch: {'ON' if state else 'OFF'}")
    def set_dehumidifier(self, state):
        log.info(f"Dehumidifier Switch: {'ON' if state else 'OFF'}")

relay = RelayBoard()

# Simple PID State Mock
class PID:
    def __init__(self, target, kp, ki, kd):
        self.target = target
        self.val = target
        self.state = False

    def tick_temp(self):
        # Simulate active thermal drift
        drift = random.uniform(-0.5, 0.5)
        # If HVAC ON -> cools aggressively. If OFF -> warms naturally.
        if self.state: self.val -= 0.6
        else: self.val += 0.2
        self.val += drift
        
        # Deadband +/- 1 C
        if self.val > self.target + 1.0 and not self.state:
            self.state = True
            relay.set_hvac(True)
        elif self.val < self.target - 1.0 and self.state:
            self.state = False
            relay.set_hvac(False)
            
        return self.val

    def tick_hum(self):
        # Simulate active humidity drift
        drift = random.uniform(-1.0, 1.0)
        if self.state: self.val -= 2.0
        else: self.val += 0.5
        self.val += drift
        
        # Target range 40-60%, Deadband
        if self.val > 55.0 and not self.state:
            self.state = True
            relay.set_dehumidifier(True)
        elif self.val < 45.0 and self.state:
            self.state = False
            relay.set_dehumidifier(False)
            
        return self.val

def main():
    log.info("Starting ECLSS Dual-PID Controller daemon...")
    pid_temp = PID(target=22.0, kp=1, ki=0, kd=0)
    pid_hum = PID(target=50.0, kp=1, ki=0, kd=0)
    
    try:
        while True:
            t = pid_temp.tick_temp()
            h = pid_hum.tick_hum()
            log.info(f"PID State -> Temp: {t:.2f}°C (Target: 22°C) | Hum: {h:.1f}% (Target: 50%)")
            time.sleep(2)  # Cycle fast for test loop
    except KeyboardInterrupt:
        log.info("Graceful exit.")

if __name__ == "__main__":
    main()
