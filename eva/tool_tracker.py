#!/usr/bin/env python3
"""
Tool tracker — RFID checkout/checkin station at the EVA airlock.

Each EVA tool carries an RFID tag. At start-up the tools listed in TOOLS_FILE are
registered with the EVA API. Every scan is then sent as CHECKOUT or CHECKIN:

  TOOL_STATION_MODE=toggle    (default) a tool that is in goes out, one that is out comes in;
                              the out-list is kept in TOOL_STATE_FILE across restarts
  TOOL_STATION_MODE=checkout  station on the way out (every scan is a CHECKOUT)
  TOOL_STATION_MODE=checkin   station on the way back in

Scans made while the MCC is unreachable are spooled and delivered later.

TOOLS_FILE is CSV with a header:  rfid_tag,tool_name,category
Reader: TOOL_RFID_DEVICE=/dev/input/by-id/... (USB reader) or TOOL_RFID_BACKEND=rc522.

Environment:
  EVA_API_URL=http://imm.local/eva  TOOLS_FILE=/etc/imm-os/tools.csv
  TOOL_STATE_FILE=/var/lib/imm-os/tools_out.json  EVA_PLAN_ID (optional)  OPERATOR_ID=airlock

  --simulate   check every mock tool out, wait 30 s, check them back in (old behaviour)
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from hw import EventPoster, simulate_requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [tool_tracker] %(message)s")
log = logging.getLogger(__name__)

API_BASE = os.getenv("EVA_API_URL", "http://localhost:8004")
OPERATOR_ID = os.getenv("OPERATOR_ID", "airlock")
EVA_PLAN_ID = int(os.getenv("EVA_PLAN_ID")) if os.getenv("EVA_PLAN_ID") else None

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


def load_tools(path: str) -> list:
    with open(path, newline="") as f:
        return [{"rfid_tag": r["rfid_tag"].strip().upper(), "tool_name": r["tool_name"].strip(),
                 "category": (r.get("category") or "").strip() or None}
                for r in csv.DictReader(f) if r.get("rfid_tag")]


def register(tools: list) -> None:
    poster = EventPoster(f"{API_BASE}/api/v1/eva/tools/register", "tool_register")
    for tool in tools:
        poster.post(tool)
    log.info("Registered %d tools", len(tools))


class ToolState:
    """Which tags are checked out; decides the action for each scan (testable)."""

    def __init__(self, mode: str, path: str = None):
        self.mode = mode
        self.path = Path(path) if path else None
        self.out = set()
        if self.path and self.path.exists():
            self.out = set(json.loads(self.path.read_text()))

    def action_for(self, tag: str) -> str:
        if self.mode == "checkout":
            return "CHECKOUT"
        if self.mode == "checkin":
            return "CHECKIN"
        return "CHECKIN" if tag in self.out else "CHECKOUT"

    def record(self, tag: str, action: str) -> None:
        (self.out.add if action == "CHECKOUT" else self.out.discard)(tag)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self.out)))


def scan(poster: EventPoster, state: ToolState, tag: str, names: dict) -> None:
    action = state.action_for(tag)
    result = poster.post({"rfid_tag": tag, "action": action, "eva_plan_id": EVA_PLAN_ID, "operator_id": OPERATOR_ID})
    label = names.get(tag, "unregistered tag")
    if result == "rejected":
        log.error("%s %s (%s) rejected by the EVA API — is it in TOOLS_FILE?", action, tag, label)
        return
    state.record(tag, action)
    log.info("%s %s (%s)%s", action, tag, label, "" if result == "ok" else " — queued, MCC unreachable")


def simulate(poster: EventPoster) -> None:
    register(MOCK_TOOLS)
    state = ToolState("toggle")
    names = {t["rfid_tag"]: t["tool_name"] for t in MOCK_TOOLS}
    for tool in MOCK_TOOLS:
        scan(poster, state, tool["rfid_tag"], names)
        time.sleep(1.0)
    log.info("All tools checked out. Simulating EVA duration (30 s)...")
    time.sleep(30)
    for tool in MOCK_TOOLS:
        scan(poster, state, tool["rfid_tag"], names)
        time.sleep(0.5)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true")
    args = p.parse_args()
    poster = EventPoster(f"{API_BASE}/api/v1/eva/tools/scan", "tool_scan")
    if simulate_requested(args.simulate):
        return simulate(poster)

    tools_file = os.getenv("TOOLS_FILE", "/etc/imm-os/tools.csv")
    tools = load_tools(tools_file) if os.path.exists(tools_file) else []
    if tools:
        register(tools)
    else:
        log.warning("No tool manifest at %s; only tools already registered can be scanned", tools_file)
    names = {t["rfid_tag"]: t["tool_name"] for t in tools}
    mode = os.getenv("TOOL_STATION_MODE", "toggle").lower()
    state = ToolState(mode, os.getenv("TOOL_STATE_FILE", "/var/lib/imm-os/tools_out.json"))

    from tag_readers import open_tag_reader
    log.info("Tool station ready (%s mode, %d tools out)", mode, len(state.out))
    for tag in open_tag_reader("TOOL_RFID"):
        scan(poster, state, tag.strip().upper(), names)


if __name__ == "__main__":
    main()
