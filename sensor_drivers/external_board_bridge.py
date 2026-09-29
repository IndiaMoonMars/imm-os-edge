#!/usr/bin/env python3
"""
IMM-OS external sensor-board bridge: GNSS (TEL0157) + Geiger (SEN0463) board → IMM-OS.

The board (firmware/esp32-external) makes one JSON line per second. This reads it either
  over Wi-Fi:  EXT_BOARD_URL=http://<board-ip>/json   (polled once a second), or
  over USB:    EXT_BOARD_PORT=/dev/serial/by-id/...   (when both ESP32 boards are on USB, name
               this one explicitly: the internal board's bridge takes the first it finds)
and publishes each section on its own topic, like the other drivers:

    geiger  habitat/sensors/geiger/<zone>   cpm, usv_h, counts, warming   (warming: first minute)
    gnss    habitat/sensors/gnss/<zone>     fix, sats, lat, lon, alt_m, sog_kn, cog_deg
    board   habitat/sensors/board/<zone>    uptime_s, reset_reason, boot_count, i2c_err, rssi_dbm

Zone: EXT_BOARD_ZONE (default "exterior"). The Pi time-stamps each line when it arrives; the
board's GNSS UTC is sent as gnss_utc when there is a fix (the MCC compares clocks with it).
A line seen twice (Wi-Fi poll faster than the board) is published once.

Modes: stdout | mqtt | both.   --send CMD (USB only): STATUS, WIFI_SSID <name>, WIFI_PASS <pw>, WIFI_OFF
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402
import watchdog  # noqa: E402

FIELDS = {
    "geiger": ("cpm", "usv_h", "counts", "warming"),
    "gnss": ("fix", "sats", "lat", "lon", "alt_m", "sog_kn", "cog_deg"),
    "board": ("uptime_s", "reset_reason", "boot_count", "i2c_err", "rssi_dbm"),
}
INT_FIELDS = {"counts", "warming", "fix", "sats", "uptime_s", "reset_reason", "boot_count", "i2c_err", "rssi_dbm"}


def to_payloads(line: dict, now: float, zone: str):
    out = []
    for sensor, fields in FIELDS.items():
        sec = line.get(sensor)
        if not isinstance(sec, dict):
            continue
        p = {"sensor": sensor, "timestamp": round(now, 3)}
        for f in fields:
            v = sec.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                p[f] = int(v) if f in INT_FIELDS else float(v)
        if sensor == "gnss" and isinstance(sec.get("utc"), str):
            p["gnss_utc"] = sec["utc"][:24]
        if len(p) > 2:
            out.append((f"habitat/sensors/{sensor}/{zone}", p))
    return out


class Dedup:
    """The board's "ms" counter: skip a line already published; a smaller one means the board restarted."""

    def __init__(self):
        self.last = None

    def new(self, line: dict) -> bool:
        ms = line.get("ms")
        if not isinstance(ms, (int, float)):
            return True
        if ms == self.last:
            return False
        self.last = ms
        return True


def poll_http(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_http(url, publish_fn, zone, now=time.time, sleep=time.sleep, fetch=poll_http, max_loops=None):
    dedup, failing, n = Dedup(), 0, 0
    while max_loops is None or n < max_loops:
        n += 1
        watchdog.kick()                 # alive even while the board is away (that is reported, not restarted)
        try:
            line = fetch(url)
            if failing:
                print(json.dumps({"info": f"external board reachable again after {failing} failed poll(s)"}),
                      file=sys.stderr, flush=True)
            failing = 0
            if isinstance(line, dict) and dedup.new(line):
                for topic, payload in to_payloads(line, now(), zone):
                    publish_fn(payload, topic)
        except (urllib.error.URLError, OSError, ValueError) as e:
            failing += 1
            if failing in (1, 10) or failing % 60 == 0:     # say it, without flooding the journal
                print(json.dumps({"error": f"external board not answering at {url}: {e}"}), file=sys.stderr, flush=True)
        sleep(1.0)


def run_serial(port, publish_fn, zone, now=time.time):
    from esp32_bridge import open_port, parse_line
    ser = open_port(port)
    ser.reset_input_buffer()
    while True:
        watchdog.kick()
        parsed = parse_line(ser.readline().decode("utf-8", "replace"))   # 2 s timeout
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "data":
            for topic, payload in to_payloads(value, now(), zone):
                publish_fn(payload, topic)
        elif kind == "info":
            print(json.dumps({"info": f"external board: {value}"}), file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="stdout")
    ap.add_argument("--send", metavar="CMD", help="a command for the board over USB (STATUS, WIFI_SSID …)")
    args = ap.parse_args()
    zone = os.getenv("EXT_BOARD_ZONE", "exterior")
    url, port = os.getenv("EXT_BOARD_URL", ""), os.getenv("EXT_BOARD_PORT", "")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if args.send:
        if not port:
            sys.exit("--send needs the board on USB: set EXT_BOARD_PORT")
        from esp32_bridge import open_port, parse_line
        ser = open_port(port)
        ser.write(args.send.strip().encode() + b"\n")          # case kept: Wi-Fi names and passwords
        end = time.monotonic() + 5
        while time.monotonic() < end:
            p = parse_line(ser.readline().decode("utf-8", "replace"))
            if p and p[0] == "info":
                print("  board:", p[1])
        return
    publish_fn = make_publisher(args.mode, f"habitat/sensors/geiger/{zone}")
    if url:
        run_http(url, publish_fn, zone)
    elif port:
        run_serial(port, publish_fn, zone)
    else:
        print(json.dumps({"error": "set EXT_BOARD_URL=http://<board-ip>/json (Wi-Fi) or EXT_BOARD_PORT (USB) "
                                   "in /etc/imm-os/edge.env"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
