#!/usr/bin/env python3
"""
IMM-OS Blackbox Logger
Reads standard input stream (from encryption_layer.py) and continuously appends to a Protobuf binary file.
Rotates files every hour. Deletes files older than 48 hours.
"""

import sys
import json
import time
import os
import glob
from datetime import datetime
import telemetry_pb2

# Configuration
STORAGE_DIR = os.getenv("IMM_BLACKBOX_DIR", "/var/lib/imm-os/blackbox")
ROTATION_INTERVAL_SEC = 3600  # 1 hour
RETENTION_HOURS = 48

def ensure_storage_dir():
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR, exist_ok=True)

def cleanup_old_files():
    """Removes protobuf logs older than RETENTION_HOURS."""
    now = time.time()
    cutoff = now - (RETENTION_HOURS * 3600)
    
    search_pattern = os.path.join(STORAGE_DIR, "telemetry_*.pb")
    for file_path in glob.glob(search_pattern):
        if os.path.getmtime(file_path) < cutoff:
            os.remove(file_path)

def get_current_filename() -> str:
    """Generates the log filename based on the current hour."""
    now_dt = datetime.utcnow()
    # E.g. telemetry_2026-04-10_15.pb
    filename = now_dt.strftime("telemetry_%Y-%m-%d_%H.pb")
    return os.path.join(STORAGE_DIR, filename)

def main():
    ensure_storage_dir()
    
    current_chunk = telemetry_pb2.TelemetryChunk()
    records_in_chunk = 0
    last_flush_time = time.time()
    last_cleanup_time = time.time()
    
    # Process line-by-line from stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
            
        try:
            payload = json.loads(line)
            
            # Expect secure envelope from encryption layer
            data_dict = payload.get("data", {})
            sig = payload.get("sig", "unsigned")
            
            # Allow fallback if no encryption layer
            if "data" not in payload and "sig" not in payload:
                data_dict = payload
            
            # Construct protobuf record
            record = current_chunk.records.add()
            record.timestamp = data_dict.get("timestamp", int(time.time()))
            record.sensor_type = data_dict.get("sensor", "unknown")
            record.payload_json = json.dumps(data_dict)
            record.signature = sig
            
            records_in_chunk += 1
            
            now = time.time()
            
            # Flush periodically (e.g. every 5 seconds) to avoid losing too much data on crash
            if (now - last_flush_time) > 5.0 or records_in_chunk > 100:
                current_file = get_current_filename()
                with open(current_file, "ab") as f:
                    # Append serialized chunk
                    # Writing raw protobuf chunk size then the chunk to allow easy appending/parsing later
                    chunk_bytes = current_chunk.SerializeToString()
                    # Store 4-byte size header
                    f.write(len(chunk_bytes).to_bytes(4, byteorder='big'))
                    f.write(chunk_bytes)
                
                # Reset chunk
                current_chunk = telemetry_pb2.TelemetryChunk()
                records_in_chunk = 0
                last_flush_time = now
                
            # Run cleanup every hour
            if (now - last_cleanup_time) > 3600.0:
                cleanup_old_files()
                last_cleanup_time = now

        except json.JSONDecodeError:
            print(json.dumps({"error": "invalid json input to blackbox logger"}), file=sys.stderr)
        except Exception as e:
            print(json.dumps({"error": f"Blackbox error: {str(e)}"}), file=sys.stderr)

    # Flush any remaining on exit
    if records_in_chunk > 0:
        current_file = get_current_filename()
        with open(current_file, "ab") as f:
            chunk_bytes = current_chunk.SerializeToString()
            f.write(len(chunk_bytes).to_bytes(4, byteorder='big'))
            f.write(chunk_bytes)

if __name__ == '__main__':
    main()
