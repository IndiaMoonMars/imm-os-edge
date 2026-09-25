#!/usr/bin/env python3
"""
IMM-OS barcode listener (Phase 11) — USB HID barcode scanner → Inventory API.

A USB barcode scanner behaves like a keyboard: it "types" the code and presses
Enter. Two ways to read it:

  --device /dev/input/by-id/usb-<scanner>-event-kbd
        Read the scanner directly via Linux evdev and grab it, so scans don't
        also land in whatever window has keyboard focus. Needs read access to
        the device (root, or the 'input' group).
  --stdin
        Read lines from the terminal (scanner types into it). Handy for testing.

Modes:
  lookup    (default) show item name, stock and location
  checkout  --crew ev1 --activity EVA-03    check a tool out
  checkin                                   check a tool back in

Authenticates as the edge node (Keycloak client imm-edge, see core/auth_client.py).
Environment: INVENTORY_API_URL (default http://imm.local/inventory), plus the
KEYCLOAK_TOKEN_URL / IMM_EDGE_CLIENT_SECRET settings in /etc/imm-os/edge.env.
"""
import argparse
import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from auth_client import auth_headers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [barcode] %(message)s")
log = logging.getLogger(__name__)

API = os.getenv("INVENTORY_API_URL", "http://imm.local/inventory").rstrip("/")
session = requests.Session()  # keep-alive: repeat scans skip the TCP/TLS handshake


def handle(code: str, mode: str, crew: str = None, activity: str = None) -> None:
    code = code.strip()
    if not code:
        return
    t0 = time.perf_counter()
    try:
        if mode == "lookup":
            r = session.get(f"{API}/api/v1/inventory/scan/{code}", headers=auth_headers(), timeout=5)
        elif mode == "checkout":
            r = session.post(f"{API}/api/v1/inventory/checkout", headers=auth_headers(), timeout=5,
                             json={"barcode": code, "crew_id": crew, "activity": activity})
        else:
            r = session.post(f"{API}/api/v1/inventory/checkin", headers=auth_headers(), timeout=5,
                             json={"barcode": code})
    except requests.RequestException as e:
        print(f"✗ {code}: API unreachable ({e.__class__.__name__})", flush=True)
        return
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:120]
        print(f"✗ {code}: {detail} [HTTP {r.status_code}, {ms:.0f} ms]", flush=True)
        return
    d = r.json()
    if mode == "lookup":
        flag = "  ⚠ LOW STOCK" if d.get("low_stock") else ""
        out = d.get("checked_out")
        out_s = f"  (checked out to {out['crew_id']} for {out['activity']})" if out else ""
        print(f"✓ {d['name']}: {d['quantity']:g} {d['unit']} @ {d.get('location') or '-'}"
              f"{flag}{out_s} [{ms:.0f} ms]", flush=True)
    elif mode == "checkout":
        print(f"✓ OUT  {d['item_name']} → {d['crew_id']} ({d['activity']}) [{ms:.0f} ms]", flush=True)
    else:
        mins = (d.get("duration_seconds") or 0) / 60
        print(f"✓ IN   {d['item_name']} after {mins:.1f} min [{ms:.0f} ms]", flush=True)


# ── Input sources ─────────────────────────────────────────────────

def read_stdin():
    for line in sys.stdin:
        yield line


def read_evdev(path: str):
    from evdev import InputDevice, categorize, ecodes  # Linux only (pip install evdev)

    chars = {**{f"KEY_{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
             **{f"KEY_{d}": d for d in "0123456789"},
             "KEY_MINUS": "-", "KEY_DOT": ".", "KEY_SLASH": "/", "KEY_SPACE": " ",
             "KEY_EQUAL": "=", "KEY_COMMA": ",", "KEY_SEMICOLON": ";"}
    shifted = {"KEY_MINUS": "_", "KEY_SLASH": "?", "KEY_EQUAL": "+", "KEY_SEMICOLON": ":"}
    dev = InputDevice(path)
    dev.grab()  # scans go only to us, not to the focused window
    log.info(f"Reading scanner {dev.name} ({path})")
    buf, shift = [], False
    try:
        for event in dev.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            key = categorize(event)
            name = key.keycode if isinstance(key.keycode, str) else key.keycode[0]
            if name in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                shift = key.keystate != key.key_up
                continue
            if key.keystate != key.key_down:
                continue
            if name in ("KEY_ENTER", "KEY_KPENTER"):
                yield "".join(buf)
                buf = []
            elif name in chars:
                c = shifted.get(name) if shift and name in shifted else chars[name]
                buf.append(c.upper() if shift and c.isalpha() else c)
    finally:
        dev.ungrab()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--device", help="evdev input device of the scanner")
    src.add_argument("--stdin", action="store_true", help="read barcodes from the terminal")
    p.add_argument("--mode", choices=("lookup", "checkout", "checkin"), default="lookup")
    p.add_argument("--crew", help="crew ID taking the tool (checkout mode)")
    p.add_argument("--activity", help="activity, e.g. EVA-03 (checkout mode)")
    args = p.parse_args()
    if args.mode == "checkout" and not (args.crew and args.activity):
        p.error("checkout mode needs --crew and --activity")

    log.info(f"Mode {args.mode}; API {API}")
    source = read_evdev(args.device) if args.device else read_stdin()
    for code in source:
        handle(code, args.mode, args.crew, args.activity)


if __name__ == "__main__":
    main()
