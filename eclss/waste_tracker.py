#!/usr/bin/env python3
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [waste_tracker] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCKS (HX711 Load Cell + RC522 RFID)
class HX711_Mock:
    def get_weight(self):
        # Base weight + some random variance representing new trash
        return 1.0  # kg

class RC522_Mock:
    def read_tag(self):
        return "TAG_FOOD_WASTE_01"

hx = HX711_Mock()
rfid = RC522_Mock()

API_URL = "http://localhost:8003/api/v1/waste/log"

def main():
    log.info("Starting Waste Tracker Daemon")
    while True:
        weight = hx.get_weight()
        tag = rfid.read_tag()
        
        # In reality, only post if weight changes significantly from tare
        log.info(f"Reading: {weight} kg | Tag: {tag}")
        
        try:
            requests.post(API_URL, json={
                "weight_kg": weight,
                "rfid_tag": tag,
                "container": "main_bin"
            })
        except Exception as e:
            log.warning("Could not reach API. Edge offline?")
            
        time.sleep(30)

if __name__ == "__main__":
    main()
