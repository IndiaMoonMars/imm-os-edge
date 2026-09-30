#!/usr/bin/env python3
"""
IMM-OS blackbox replay: send readings from the node's blackbox to the MCC again.

Every reading a sensor pipeline produces is also written to the local blackbox
(/var/lib/imm-os/blackbox, 48 h). Normally the local broker's queue already carries
everything across an MCC or network outage; replay fills what that could not: the MCC
reports the gap (lost readings per stream, from the sequence numbers), and you replay
that window.

Each reading is published again on its own topic through the node's broker, unchanged
except for "delayed": true. It keeps its timestamp, sequence number and run, so at the
MCC it lands exactly where it was measured, overwrites rather than duplicates anything
that did arrive, is counted as recovered in the stream's gap, and raises no live alarm.

  .venv/bin/python core/blackbox_replay.py --since 2026-09-28T09:10 --until 2026-09-28T09:40
  .venv/bin/python core/blackbox_replay.py --since 1790580000 --sensor scd40 --dry-run
"""
import argparse
import glob
import json
import logging
import os
import struct
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [replay] %(message)s")
log = logging.getLogger(__name__)

BLACKBOX_DIR = os.getenv("IMM_BLACKBOX_DIR", "/var/lib/imm-os/blackbox")


def read_chunks(file_path: str):
    """Yield TelemetryChunk objects from a length-prefixed .pb file (stops at a torn tail)."""
    import telemetry_pb2
    with open(file_path, "rb") as f:
        while True:
            header = f.read(4)
            if len(header) < 4:
                break
            chunk_len = struct.unpack(">I", header)[0]
            chunk_bytes = f.read(chunk_len)
            if len(chunk_bytes) < chunk_len:
                break
            chunk = telemetry_pb2.TelemetryChunk()
            chunk.ParseFromString(chunk_bytes)
            yield chunk


def topic_for(reading: dict):
    """The MQTT topic the reading was first published on, or None."""
    sensor = reading.get("sensor")
    if sensor == "eva_biosensor":
        crew = reading.get("crew_id")
        return f"habitat/eva/biosensors/{crew}" if crew else None
    zone = reading.get("zone")
    if not sensor or not zone or "/" in sensor or "/" in str(zone):
        return None
    return f"habitat/sensors/{sensor}/{zone}"


def readings(directory: str, since: float, until: float, sensor: str = None):
    """(topic, payload) for every blackbox reading in [since, until], oldest file first."""
    for path in sorted(glob.glob(os.path.join(directory, "telemetry_*.pb"))):
        try:
            for chunk in read_chunks(path):
                for record in chunk.records:
                    if not since - 1 <= record.timestamp <= until:
                        continue
                    try:
                        data = json.loads(record.payload_json)
                    except ValueError:
                        continue
                    ts = data.get("timestamp", record.timestamp)
                    if not isinstance(ts, (int, float)) or not since <= ts <= until:
                        continue
                    if sensor and data.get("sensor") != sensor:
                        continue
                    topic = topic_for(data)
                    if topic:
                        yield topic, {**data, "delayed": True}
        except Exception as exc:      # a damaged file must not stop the rest
            log.error("Error reading %s: %s", path, exc)


def parse_time(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        dt = datetime.fromisoformat(text)
        return (dt if dt.tzinfo else dt.astimezone()).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="start: Unix time or ISO time (local time unless it has a zone)")
    ap.add_argument("--until", default=None, help="end (default: now)")
    ap.add_argument("--sensor", help="only this sensor, e.g. scd40")
    ap.add_argument("--rate", type=float, default=200.0, help="readings per second (default 200)")
    ap.add_argument("--dir", default=BLACKBOX_DIR)
    ap.add_argument("--dry-run", action="store_true", help="count what would be sent")
    args = ap.parse_args()
    since = parse_time(args.since)
    until = parse_time(args.until) if args.until else time.time()
    todo = readings(args.dir, since, until, args.sensor)
    window = f"{datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d %H:%M:%S} to " \
             f"{datetime.fromtimestamp(until, timezone.utc):%H:%M:%S} UTC"
    if args.dry_run:
        n = sum(1 for _ in todo)
        print(f"{n} reading(s) in the blackbox for {window}")
        return 0

    from mqtt_publisher import create_client
    client = create_client()
    for _ in range(100):
        if client.is_connected():
            break
        time.sleep(0.1)
    else:
        log.error("Cannot reach the broker at %s:%s", os.getenv("MQTT_HOST", "localhost"), os.getenv("MQTT_PORT", "1883"))
        return 1
    sent, pending = 0, []
    for topic, payload in todo:
        pending.append(client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1))
        sent += 1
        if len(pending) >= 100:
            for info in pending:
                info.wait_for_publish()
            pending.clear()
        time.sleep(1.0 / args.rate)
    for info in pending:
        info.wait_for_publish()
    client.loop_stop()
    client.disconnect()
    log.info("Replayed %d reading(s) for %s (marked delayed)", sent, window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
