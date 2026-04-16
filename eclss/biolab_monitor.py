#!/usr/bin/env python3
import time
import requests
import logging
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s [biolab_monitor] %(message)s")
log = logging.getLogger(__name__)

# MOCKS (Atlas Scientific pH Probe + DS18B20 Temp)
def read_pH():
    return 7.0 + random.uniform(-0.1, 0.1)

def read_water_temp():
    return 24.5 + random.uniform(-0.2, 0.2)

API_URL = "http://localhost:8003/api/v1/biolab/log"

def main():
    log.info("Starting Biolab I2C Interface Daemon")
    
    while True:
        ph = read_pH()
        temp = read_water_temp()
        
        log.info(f"Biolab Samples -> pH: {ph:.2f} | Water Temp: {temp:.2f}°C")
        
        try:
            requests.post(API_URL, json={
                "ph_level": round(ph, 2),
                "water_temp_c": round(temp, 2)
            })
        except Exception as e:
            log.warning("Could not reach API. Edge offline?")
            
        time.sleep(60)

if __name__ == "__main__":
    main()
