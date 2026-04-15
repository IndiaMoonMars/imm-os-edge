#!/usr/bin/env python3
"""
IMM-OS Encryption Layer
Wraps incoming telemetry JSON payloads with an HMAC-SHA256 signature using a shared secret.

Reads from stdin line-by-line, outputs to stdout line-by-line.
Format: {"data": <original_payload>, "sig": "<64-char_hex_hmac>"}
"""

import sys
import json
import hmac
import hashlib
import os

# During Phase 1, we rely on a shared environment secret.
SECRET_KEY = os.getenv("IMM_SECRET_KEY", "default-dev-secret").encode('utf-8')

def sign_payload(data: dict) -> str:
    """Generates an HMAC-SHA256 signature from a consistently ordered JSON string of data."""
    # Serialize the data payload uniformly to ensure identical signatures
    msg_str = json.dumps(data, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return hmac.new(SECRET_KEY, msg_str, hashlib.sha256).hexdigest()

def verify_signature(data: dict, sig: str) -> bool:
    """Verifies that the given payload's signature matches."""
    expected_sig = sign_payload(data)
    # Use compare_digest to prevent timing attacks
    return hmac.compare_digest(expected_sig, sig)

def main():
    # Process line-by-line from stdin (pipeline support)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
            
        try:
            payload = json.loads(line)
            
            # If there's an error passed from the sensor driver, just pass it through unmodified
            if "error" in payload:
                print(json.dumps(payload), flush=True)
                continue
                
            sig = sign_payload(payload)
            secure_envelope = {
                "data": payload,
                "sig": sig
            }
            
            print(json.dumps(secure_envelope), flush=True)
            
        except json.JSONDecodeError:
            # Handle non-JSON lines gracefully
            print(json.dumps({"error": "invalid json input to encryption layer"}), file=sys.stderr)
        except Exception as e:
            # Drop malformed logs but print internal error
            print(json.dumps({"error": f"Encryption error: {str(e)}"}), file=sys.stderr)

if __name__ == '__main__':
    main()
