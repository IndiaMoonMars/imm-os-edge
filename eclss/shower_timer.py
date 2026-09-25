#!/usr/bin/env python3
import time
import requests
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from auth_client import auth_headers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [shower_timer] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (PIR Motion Array)
ECLSS_API_URL = os.getenv("ECLSS_API_URL", "http://localhost:8003")
API_URL = f"{ECLSS_API_URL}/api/v1/water/shower"

def main():
    log.info("Starting PIR Shower Timer Daemon")
    # Simulate a PIR sensor activating, staying ON for 3 minutes, then dropping.
    log.info("PIR Triggered! Tracking shower flow...")
    
    # 3 mins duration = 180 seconds. Using sleep for simulation speed.
    shower_duration_seconds = 180.0
    estimated_water_liters = 45.0  # (3 minutes * ~15L/min standard)
    
    log.info(f"PIR Dropped! Tracked {shower_duration_seconds/60:.1f} minute shower. Estimated {estimated_water_liters}L drawn.")
    
    try:
        resp = requests.post(API_URL, headers=auth_headers(), timeout=5, json={
            "duration_seconds": shower_duration_seconds,
            "estimated_liters": estimated_water_liters
        })
        if resp.status_code >= 400:
            log.warning(f"API rejected event: HTTP {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        log.warning("Could not reach API. Edge offline?")

if __name__ == "__main__":
    main()
