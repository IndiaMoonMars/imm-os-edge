#!/usr/bin/env python3
import time
import requests
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from auth_client import auth_headers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [water_monitor] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (YF-S201 Flow Meter)
def simulate_water_draw():
    # Simulate someone drawing exactly 500 mL of water
    return 500.0

ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/water/log"

def main():
    log.info("Starting Daily Water Flow Monitor Daemon")
    daily_accumulated_ml = 0.0
    
    while True:
        flow_event = simulate_water_draw()
        daily_accumulated_ml += flow_event
        
        log.info(f"Registered {flow_event} mL flow event. Daily total: {daily_accumulated_ml} mL")
        
        try:
            resp = requests.post(API_URL, headers=auth_headers(), timeout=5, json={
                "event_ml": flow_event,
                "daily_total_ml": daily_accumulated_ml,
                "source": "drinking_line"
            })
            if resp.status_code >= 400:
                log.warning(f"API rejected event: HTTP {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            log.warning("Could not reach API. Edge offline?")
            
        time.sleep(60) # Simulate an event every minute for debug

if __name__ == "__main__":
    main()
