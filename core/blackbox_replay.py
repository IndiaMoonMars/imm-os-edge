#!/usr/bin/env python3
"""
IMM-OS Blackbox Replay
Reads accumulated .pb blackbox files and replays them to Kafka 'telemetry.raw'.
Use this after a network reconnect to fill any data gaps.
Deduplication key: {sensor}:{timestamp} (set as Kafka message key).
"""

import os
import sys
import json
import glob
import struct
import logging
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from confluent_kafka import Producer

# Generated from telemetry.proto
try:
    import telemetry_pb2
except ImportError:
    sys.exit("Run compile_proto.sh first to generate telemetry_pb2.py")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [replay] %(message)s")
log = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
RAW_TOPIC       = "telemetry.raw"
BLACKBOX_DIR    = os.getenv("IMM_BLACKBOX_DIR", "/var/lib/imm-os/blackbox")


def read_chunks(file_path: str):
    """Generator: yields TelemetryChunk objects from a length-prefixed .pb file."""
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


def replay(since_ts: int = 0):
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "acks": "all"})
    replayed = 0
    skipped  = 0

    pb_files = sorted(glob.glob(os.path.join(BLACKBOX_DIR, "telemetry_*.pb")))
    if not pb_files:
        log.warning("No .pb files found in %s", BLACKBOX_DIR)
        return

    log.info("Replaying %d blackbox file(s) since ts=%d", len(pb_files), since_ts)

    for pb_file in pb_files:
        log.info("Reading %s", pb_file)
        try:
            for chunk in read_chunks(pb_file):
                for record in chunk.records:
                    if record.timestamp < since_ts:
                        skipped += 1
                        continue

                    # Use sensor:timestamp as dedup key
                    key = f"{record.sensor_type}:{record.timestamp}".encode()

                    producer.produce(
                        RAW_TOPIC,
                        key=key,
                        value=record.payload_json.encode(),
                    )
                    producer.poll(0)
                    replayed += 1
        except Exception as e:
            log.error("Error reading %s: %s", pb_file, e)

    producer.flush(timeout=30)
    log.info("Replay complete — %d records replayed, %d skipped (before since_ts)", replayed, skipped)


def main():
    parser = argparse.ArgumentParser(description="Replay IMM-OS blackbox to Kafka")
    parser.add_argument("--since", type=int, default=0,
                        help="Unix timestamp — only replay records after this time")
    args = parser.parse_args()
    replay(since_ts=args.since)


if __name__ == "__main__":
    main()
