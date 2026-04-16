#!/usr/bin/env python3
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [shower_timer] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (PIR Motion Array)
API_URL = "http://localhost:8003/api/v1/water/shower"

def main():
    log.info("Starting PIR Shower Timer Daemon")
    # Simulate a PIR sensor activating, staying ON for 3 minutes, then dropping.
    log.info("PIR Triggered! Tracking shower flow...")
    
    # 3 mins duration = 180 seconds. Using sleep for simulation speed.
    shower_duration_seconds = 180.0
    estimated_water_liters = 45.0  # (3 minutes * ~15L/min standard)
    
    log.info(f"PIR Dropped! Tracked {shower_duration_seconds/60:.1f} minute shower. Estimated {estimated_water_liters}L drawn.")
    
    try:
        requests.post(API_URL, json={
            "duration_seconds": shower_duration_seconds,
            "estimated_liters": estimated_water_liters
        })
    except Exception as e:
        log.warning("Could not reach API. Edge offline?")

if __name__ == "__main__":
    main()
