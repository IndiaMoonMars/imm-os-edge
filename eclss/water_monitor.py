#!/usr/bin/env python3
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [water_monitor] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (YF-S201 Flow Meter)
def simulate_water_draw():
    # Simulate someone drawing exactly 500 mL of water
    return 500.0

API_URL = "http://localhost:8003/api/v1/water/log"

def main():
    log.info("Starting Daily Water Flow Monitor Daemon")
    daily_accumulated_ml = 0.0
    
    while True:
        flow_event = simulate_water_draw()
        daily_accumulated_ml += flow_event
        
        log.info(f"Registered {flow_event} mL flow event. Daily total: {daily_accumulated_ml} mL")
        
        try:
            requests.post(API_URL, json={
                "event_ml": flow_event,
                "daily_total_ml": daily_accumulated_ml,
                "source": "drinking_line"
            })
        except Exception as e:
            log.warning("Could not reach API. Edge offline?")
            
        time.sleep(60) # Simulate an event every minute for debug

if __name__ == "__main__":
    main()
