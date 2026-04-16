#!/usr/bin/env python3
"""
Tool Tracker — RFID checkout/checkin station at EVA airlock.
Scans an RFID tag and POSTs to the EVA API (action: CHECKOUT or CHECKIN).
In simulation mode cycles through a list of mock tool tags.
"""
import time
import json
import logging
import requests
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [tool_tracker] %(message)s")
log = logging.getLogger(__name__)

API_BASE    = os.getenv("EVA_API_URL", "http://localhost:8004")
OPERATOR_ID = os.getenv("OPERATOR_ID", "MCC-Lead")
EVA_PLAN_ID = int(os.getenv("EVA_PLAN_ID", 1))

# 20+ mock tool RFID tags (in production, RC522 SPI reader populates this live)
MOCK_TOOLS = [
    {"rfid_tag": f"TOOL-{str(i).zfill(3)}", "tool_name": name, "category": cat}
    for i, (name, cat) in enumerate([
        ("Torque Wrench", "Mechanical"), ("Socket Set", "Mechanical"),
        ("Cable Cutter", "Electrical"), ("Multimeter", "Electrical"),
        ("Hammer Drill", "Mechanical"), ("Heat Gun", "Thermal"),
        ("Patch Kit", "Repair"), ("Sealant Gun", "Repair"),
        ("Bolt Driver", "Mechanical"), ("Sample Collector", "Science"),
        ("Soil Probe", "Science"), ("Radiation Badge", "Safety"),
        ("Emergency Beacon", "Safety"), ("Tether Hook", "Safety"),
        ("Wire Bundle", "Electrical"), ("Zip Ties", "Misc"),
        ("Duct Tape", "Misc"), ("Flashlight", "Misc"),
        ("Mirror Signal", "Safety"), ("Geology Hammer", "Science"),
        ("Core Drill", "Science"), ("Carabiner Set", "Safety"),
    ], 1)
]

def ensure_tools_registered():
    """Register all tools in EVA API inventory if not already present."""
    for tool in MOCK_TOOLS:
        try:
            requests.post(f"{API_BASE}/api/v1/eva/tools/register", json=tool, timeout=3)
        except Exception:
            pass

def scan_cycle():
    """Simulate checkout-then-checkin of each tool over time."""
    ensure_tools_registered()
    for tool in MOCK_TOOLS:
        tag = tool["rfid_tag"]
        # Checkout
        resp = requests.post(f"{API_BASE}/api/v1/eva/tools/scan", json={
            "rfid_tag": tag,
            "action": "CHECKOUT",
            "eva_plan_id": EVA_PLAN_ID,
            "operator_id": OPERATOR_ID
        }, timeout=5)
        log.info(f"CHECKOUT {tag} ({tool['tool_name']}) → {resp.status_code}")
        time.sleep(1.0)

    log.info("All tools checked out. Simulating EVA duration (30 s)...")
    time.sleep(30)

    for tool in MOCK_TOOLS:
        tag = tool["rfid_tag"]
        resp = requests.post(f"{API_BASE}/api/v1/eva/tools/scan", json={
            "rfid_tag": tag,
            "action": "CHECKIN",
            "eva_plan_id": EVA_PLAN_ID,
            "operator_id": OPERATOR_ID
        }, timeout=5)
        log.info(f"CHECKIN  {tag} ({tool['tool_name']}) → {resp.status_code}")
        time.sleep(0.5)

if __name__ == "__main__":
    scan_cycle()
