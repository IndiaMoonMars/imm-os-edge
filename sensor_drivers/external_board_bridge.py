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

Zone: EXT_BOARD_ZONE (default "exterior"; not replaced by the node's IMM_ZONE). The Pi
time-stamps each line when it arrives; the board's GNSS UTC is sent as gnss_utc when there is
a fix. A line seen twice (Wi-Fi poll faster than the board) is published once.

Boards running other firmware (for example a dashboard of your own) work too, without
reflashing: any JSON the board serves, or even a page that shows the values as text
("CPM: 24", "Latitude: 19.07601"), is read by recognising the usual names (cpm, usv/h, dose,
lat/latitude, lon/lng/longitude, alt, sats/satellites, speed, course, fix, rssi …). Names it
can't guess: EXT_BOARD_MAP="cpm=rad.count,lat=gps.y" (field = dotted path in the board's JSON).

    --probe [URL]   what the board serves and which values are recognised; if the page loads its
                    data by script, finds the data URL in it and tries that (use it as EXT_BOARD_URL)
    --find          look for the board on this Pi's local networks

On USB (EXT_BOARD_PORT, speed EXT_BOARD_BAUD, default 115200) the same goes for what the board
prints: JSON lines, or text such as "CPM: 24" / "Lat: 19.07601 Lon: 72.87765".
    --listen [PORT] what each USB serial board prints, at the usual speeds, and what is recognised

Modes: stdout | mqtt | both.   --send CMD (USB, IMM-OS firmware): STATUS, WIFI_SSID <name>, WIFI_PASS <pw>, WIFI_OFF
"""
import argparse
import html
import http.client
import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import watchdog  # noqa: E402   (mqtt_publisher is imported in main: --probe/--find need no paho)

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
        p = {"sensor": sensor, "timestamp": round(now, 3), "zone": zone}     # zone set: IMM_ZONE doesn't replace it
        for f in fields:
            v = sec.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                p[f] = int(v) if f in INT_FIELDS else float(v)
        if sensor == "gnss" and isinstance(sec.get("utc"), str):
            p["gnss_utc"] = sec["utc"][:24]
        if len(p) > 3:
            out.append((f"habitat/sensors/{sensor}/{zone}", p))
    return out


# ── Any board firmware: recognise the values by name ──────────────────
# (section, field, pattern on the name lower-cased with only letters and digits, factor)
RULES = [
    ("geiger", "cpm", r"(geiger|radiation|rad|gm)?(cpm|countsperminute|countspermin|clicksperminute)", 1.0),
    ("geiger", "usv_h", r"(geiger|radiation|rad|gm|dose|doserate)?(usvh|usvhr|usv|usvperh|usvperhour|microsieverts?(perhour|h)?|"
                        r"usieverts?(perhour|h)?|doserate|dose|svrate|usvrate)", 1.0),
    ("geiger", "counts", r"(geiger)?(counts|count|pulses|totalcounts|totalcount|clicks)", 1.0),
    ("gnss", "lat", r"(gps|gnss)?(lat|latitude|latitudedegree|latdeg)", 1.0),
    ("gnss", "lon", r"(gps|gnss)?(lon|lng|long|longitude|longitudedegree|londeg|lngdeg)", 1.0),
    ("gnss", "alt_m", r"(gps|gnss)?(alt|altitude|altm|altitudem)", 1.0),
    ("gnss", "sats", r"(gps|gnss)?(sats|satellites|satellitesused|satsused|numsat|numsats|satnum|satcount|satsinuse|"
                     r"satellitesinuse|starnum|usedstar|numsatused|satellitecount)", 1.0),
    ("gnss", "sog_kn", r"(gps|gnss)?(sog|sogkn|speedkn|speedknots?|knots)", 1.0),
    ("gnss", "sog_kn", r"(gps|gnss)?(speedkmh|speedkph|kmh|kph|speedkmhr)", 1 / 1.852),
    ("gnss", "sog_kn", r"(gps|gnss)?(speedms|speedmps|mps)", 1.943844),
    ("gnss", "cog_deg", r"(gps|gnss)?(cog|cogdeg|course|coursedeg|courseoverground)", 1.0),
    ("gnss", "fix", r"(gps|gnss)?(fix|hasfix|fixed|valid|fixvalid|isvalid|fixok|locationvalid|fixquality|fixstatus)", 1.0),
    ("board", "rssi_dbm", r"(wifi)?(rssi|rssidbm|wifirssi|signaldbm)", 1.0),
    ("board", "uptime_s", r"(uptime|uptimes|uptimesec|uptimeseconds)", 1.0),
]
TEXT_RULES = [   # strings
    ("gnss", "utc", r"(gps|gnss)?(utc|utctime|datetime|time|timestamp)"),
    ("gnss", "lat_dir", r"(gps|gnss)?(latdir|latdirection|ns|latns|lathemisphere)"),
    ("gnss", "lon_dir", r"(gps|gnss)?(londir|londirection|ew|lonew|lonhemisphere)"),
]
_RULES = [(sec, f, re.compile(f"^(?:{pat})$"), k) for sec, f, pat, k in RULES]
_TEXT_RULES = [(sec, f, re.compile(f"^(?:{pat})$")) for sec, f, pat in TEXT_RULES]
_NUM = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:°|deg)?\s*([NSEWnsew])?\b")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("µ", "u").replace("μ", "u"))


def _number(v):
    """(value, hemisphere letter or None) from a number, bool or text like "19.07601 N"."""
    if isinstance(v, bool):
        return float(v), None
    if isinstance(v, (int, float)):
        return float(v), None
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "yes", "ok", "valid", "fix", "3d", "2d"):
            return 1.0, None
        if low in ("false", "no", "none", "invalid", "nofix", "no fix", "-", ""):
            return 0.0, None
        m = _NUM.match(v)
        if m:
            return float(m.group(1)), (m.group(2) or "").upper() or None
    return None, None


def flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = obj
    return out


def parse_map(spec: str) -> dict:
    """EXT_BOARD_MAP "cpm=rad.count, lat=gps.y" → {"cpm": "rad.count", …}."""
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            f, path = (x.strip() for x in part.split("=", 1))
            if f and path:
                out[f] = path
    return out


def _section_of(field: str) -> str:
    for sec, fields in FIELDS.items():
        if field in fields:
            return sec
    return "gnss" if field in ("utc", "lat_dir", "lon_dir") else ""


def recognise(obj, mapping: dict = None) -> dict:
    """Any board's data (JSON object, or {label: text} from a page) → this bridge's line format."""
    if isinstance(obj, dict) and "ms" in obj and not mapping and \
            (isinstance(obj.get("geiger"), dict) or isinstance(obj.get("gnss"), dict)):
        return obj                                             # IMM-OS firmware (firmware/esp32-external)
    flat = flatten(obj) if isinstance(obj, (dict, list)) else {}
    line = {"geiger": {}, "gnss": {}, "board": {}}
    extra = {}
    by_path = {p.lower(): v for p, v in flat.items()}
    for field, path in (mapping or {}).items():
        v = by_path.get(path.lower())
        sec = _section_of(field)
        if v is None or not sec:
            continue
        if field in ("utc", "lat_dir", "lon_dir"):
            (extra if field != "utc" else line[sec])[field] = str(v)
        else:
            n, hemi = _number(v)
            if n is not None:
                line[sec][field] = n
                if hemi:
                    extra[f"{field}_hemi"] = hemi
    taken = set((mapping or {}).values())
    for path, v in flat.items():
        if path in taken:
            continue
        leaf = _key(path.rsplit(".", 1)[-1])
        parent = _key(path.rsplit(".", 2)[-2]) if "." in path else ""
        hit = False
        for sec, field, rx, factor in _RULES:
            if (rx.match(leaf) or (parent in ("gps", "gnss", "geiger", "radiation") and rx.match(parent + leaf))) \
                    and field not in line[sec]:
                n, hemi = _number(v)
                if n is not None:
                    line[sec][field] = n * factor
                    if hemi:
                        extra[f"{field}_hemi"] = hemi
                hit = True
                break
        if hit or not isinstance(v, str):
            continue
        for sec, field, rx in _TEXT_RULES:
            if rx.match(leaf):
                if field == "utc":
                    if re.search(r"\d[:T-]\d", v):                 # a time, not just a number
                        line[sec].setdefault("utc", v.strip())
                else:
                    extra.setdefault(field, v.strip().upper()[:1])
                break
    g = line["gnss"]
    for f, dir_key, neg in (("lat", "lat_dir", "S"), ("lon", "lon_dir", "W")):
        if f not in g:
            continue
        v, lim = g[f], (90 if f == "lat" else 180)
        if abs(v) > lim and abs(v) <= lim * 100:                  # NMEA ddmm.mmmm, not degrees
            v = (int(abs(v) / 100) + (abs(v) % 100) / 60) * (1 if v >= 0 else -1)
        hemi = extra.get(f"{f}_hemi") or extra.get(dir_key)
        if hemi == neg and v > 0:
            v = -v
        g[f] = round(v, 7)
    if "fix" in g:
        g["fix"] = 1 if g["fix"] > 0 else 0
    if g.get("lat") == 0 and g.get("lon") == 0:                   # a board without a fix often shows 0, 0
        g.pop("lat"), g.pop("lon")
        g.setdefault("fix", 0)
    if "fix" not in g and ("lat" in g or "sats" in g):
        g["fix"] = 1 if "lat" in g and "lon" in g else 0
    if not g.get("fix"):
        for f in ("lat", "lon", "alt_m", "sog_kn", "cog_deg"):      # no fix: no position
            g.pop(f, None)
    geo = line["geiger"]
    if "cpm" in geo and "usv_h" not in geo:
        geo["usv_h"] = round(geo["cpm"] / 153.8, 4)               # M4011 (SEN0463), as the IMM-OS firmware
    elif "usv_h" in geo and "cpm" not in geo:
        geo["cpm"] = round(geo["usv_h"] * 153.8, 1)
    return {k: v for k, v in line.items() if v}


_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_PAIR = re.compile(r"([A-Za-zµμ][A-Za-z0-9µμ /()._%-]{0,40}?)\s*[:=]\s*"
                   r"([-+]?\d+(?:\.\d+)?\s*(?:°|deg)?\s*[NSEWnsew]?\b|true|false|yes|no)", re.I)


def text_pairs(text: str) -> dict:
    """ "CPM: 24<br>Latitude: 19.07601 N" → {"CPM": "24", "Latitude": "19.07601 N"} (a page's visible text)."""
    plain = html.unescape(_TAG.sub("\n", text))
    out = {}
    for label, value in _PAIR.findall(plain):
        label = label.strip()
        if "(" in label and ")" not in label:
            label = label.split("(")[0].strip()
        out.setdefault(label, value.strip())
    return out


def parse_body(text: str):
    """The board's answer as data: JSON if it is JSON, else the label: value pairs in its text."""
    t = text.strip()
    if t[:1] in "{[":
        try:
            return json.loads(t)
        except ValueError:
            pass
    return text_pairs(t)


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


FETCH_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)


def get_raw(url: str, timeout: float = 3.0) -> str:
    """GET for small hand-written servers that send the page without an HTTP status line and
    headers (common in ESP32 WiFiServer sketches): read until the board closes the connection."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    with socket.create_connection((u.hostname, u.port or 80), timeout=timeout) as c:
        c.sendall(f"GET {path} HTTP/1.0\r\nHost: {u.hostname}\r\nConnection: close\r\n\r\n".encode())
        chunks, total, end = [], 0, time.monotonic() + timeout
        while total < 512 * 1024 and time.monotonic() < end:
            try:
                b = c.recv(4096)
            except socket.timeout:
                break                                   # some sketches never close: keep what arrived
            if not b:
                break
            chunks.append(b)
            total += len(b)
    text = b"".join(chunks).decode("utf-8", "replace")
    if text.startswith("HTTP/"):                        # headers after all: drop them
        text = text.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in text else text.split("\n\n", 1)[-1]
    return text


def get_text(url: str, timeout: float = 3.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(512 * 1024).decode("utf-8", "replace")
    except (http.client.BadStatusLine, http.client.RemoteDisconnected):
        return get_raw(url, timeout)


def poll_http(url: str, timeout: float = 3.0):
    return parse_body(get_text(url, timeout))


def run_http(url, publish_fn, zone, now=time.time, sleep=time.sleep, fetch=poll_http, max_loops=None, mapping=None):
    dedup, failing, n, empty = Dedup(), 0, 0, 0
    while max_loops is None or n < max_loops:
        n += 1
        watchdog.kick()                 # alive even while the board is away (that is reported, not restarted)
        try:
            line = fetch(url)
            if failing:
                print(json.dumps({"info": f"external board reachable again after {failing} failed poll(s)"}),
                      file=sys.stderr, flush=True)
            failing = 0
            if isinstance(line, (dict, list)) and dedup.new(line if isinstance(line, dict) else {}):
                out = to_payloads(recognise(line, mapping), now(), zone)
                empty = 0 if out else empty + 1
                if empty in (5, 300):
                    print(json.dumps({"error": f"{url} answers, but with no GNSS or Geiger values in it: run "
                                               "external_board_bridge.py --probe to find the board's data URL"}),
                          file=sys.stderr, flush=True)
                for topic, payload in out:
                    publish_fn(payload, topic)
        except FETCH_ERRORS as e:
            failing += 1
            if failing in (1, 10) or failing % 60 == 0:     # say it, without flooding the journal
                print(json.dumps({"error": f"external board not answering at {url}: {e}"}), file=sys.stderr, flush=True)
        sleep(1.0)


class LineCollector:
    """Serial output of any firmware → complete readings.

    A JSON line is one reading. Text lines ("CPM: 24", "Lat: 19.07601, Lon: 72.87765") are
    gathered until a name repeats (the next round of prints has started), a blank or
    "-----" line, or a quiet moment (flush()); then they are one reading."""

    def __init__(self, mapping=None):
        self.mapping, self.pending = mapping, {}

    def flush(self):
        out, self.pending = self.pending, {}
        return recognise(out, self.mapping) if out else None

    def feed(self, line: str):
        """A reading (this bridge's line format) when one is complete, else None."""
        s = line.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                return None
            if isinstance(obj, dict):
                self.pending = {}
                return recognise(obj, self.mapping)
            return None
        if not s or set(s) <= set("-=*_#~. "):
            return self.flush()
        pairs = text_pairs(s)
        if not pairs:
            return None
        done = self.flush() if any(k in self.pending for k in pairs) else None
        self.pending.update(pairs)
        return done


def open_serial(port: str, baud: int, exclusive: bool = False):
    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, 1.0
    if exclusive:
        ser.exclusive = True                            # fails if a running service holds the port
    ser.open()
    ser.rts = False                                     # as esp32_bridge.open_port: don't reset the board
    ser.dtr = False
    return ser


def run_serial(port, publish_fn, zone, now=time.time, mapping=None, baud=115200, ser=None, max_lines=None):
    ser = ser or open_serial(port, baud)
    ser.reset_input_buffer()
    col, n, empty = LineCollector(mapping), 0, 0
    while max_lines is None or n < max_lines:
        n += 1
        watchdog.kick()
        raw = ser.readline().decode("utf-8", "replace")                 # 1 s timeout
        if raw.strip().startswith("#"):
            print(json.dumps({"info": f"external board: {raw.strip().lstrip('# ')}"}), file=sys.stderr, flush=True)
            continue
        line = col.feed(raw) if raw else col.flush()                    # quiet: what was gathered is a reading
        if not line:
            continue
        out = to_payloads(line, now(), zone)
        empty = 0 if out else empty + 1
        if empty in (5, 300):
            print(json.dumps({"error": f"{port} prints, but no GNSS or Geiger values were recognised: "
                                       "run external_board_bridge.py --listen to see its output"}),
                  file=sys.stderr, flush=True)
        for topic, payload in out:
            publish_fn(payload, topic)


def serial_ports(find=None):
    import glob
    find = find or glob.glob
    ports = sorted(find("/dev/serial/by-id/*"))
    return ports or sorted(find("/dev/ttyUSB*") + find("/dev/ttyACM*"))


def port_users(port: str, proc="/proc") -> list:
    """Other processes with this serial port open: "pid command" (Linux; own-user processes only
    unless run with sudo)."""
    target, me, users = os.path.realpath(port), os.getpid(), []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit() and int(p) != me]
    except OSError:
        return users
    for pid in pids:
        try:
            fds = os.listdir(f"{proc}/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(f"{proc}/{pid}/fd/{fd}") == target:
                    with open(f"{proc}/{pid}/cmdline", "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                    users.append(f"{pid} {cmd[:100]}")
                    break
            except OSError:
                continue
    return users


def reset_board(ser, sleep=time.sleep):
    """Restart the ESP32 through the DevKit's auto-reset circuit (EN low while RTS is on and DTR
    off), as esptool does; IO0 stays high, so it boots the sketch, not the flasher."""
    ser.dtr = False
    ser.rts = True
    sleep(0.12)
    ser.rts = False


def listen(port: str, seconds: float = 8.0, out=print, opener=None, users=port_users, reset=False,
           probe_fn=None) -> tuple:
    """Read a USB serial port at the usual speeds and show what the board prints.
    Returns (baud, reading) for the speed whose output has GNSS/Geiger values, else (0, None)."""
    opener = opener or (lambda p, b: open_serial(p, b, exclusive=True))
    probe_fn = probe_fn or probe
    held = users(port)
    if held:
        out(f"  ✗ {port} is already open in another program, which takes what the board prints:")
        for u in held:
            out(f"      {u}")
        if any("esp32_bridge.py" in u for u in held):
            out("    that is the INTERNAL board's driver, reading this board because it found no other ESP32. "
                "Stop it (sudo systemctl stop imm-sensor-pipeline@esp32_bridge.py), or set ESP32_PORT / "
                "EXT_BOARD_PORT in /etc/imm-os/edge.env")
        else:
            out("    stop it first, e.g.: sudo systemctl stop 'imm-sensor-pipeline@*'")
        return 0, None
    for baud in (115200, 9600, 57600, 38400, 74880, 19200, 230400, 250000, 460800, 921600):
        try:
            ser = opener(port, baud)
        except Exception as e:                          # busy, no permission, gone
            msg = str(e)
            if "busy" in msg.lower() or "lock" in msg.lower() or "exclusive" in msg.lower():
                out(f"  ✗ {port} is in use by a running service (the internal board's driver, if this is the "
                    "internal board; or external_board_bridge.py). Stop it first: "
                    "sudo systemctl stop 'imm-sensor-pipeline@*'")
            elif "permission" in msg.lower():
                out(f"  ✗ no permission for {port}: your user needs the dialout group (log out and in after setup-node.sh)")
            else:
                out(f"  ✗ {port}: {msg}")
            return 0, None
        lines, col, best = [], LineCollector(), None
        if reset and baud == 115200:
            out("  · restarting the ESP32 over USB (its start-up message comes at 115200 baud) …")
            reset_board(ser)
        end = time.monotonic() + (seconds if baud == 115200 else min(seconds, 3.0))   # the usual speed gets longest
        try:
            while time.monotonic() < end:
                raw = ser.readline().decode("utf-8", "replace")
                if raw.strip():
                    lines.append(raw.rstrip())
                line = col.feed(raw) if raw else col.flush()
                if line and score(line) > score(best or {}):
                    best = line
            last = col.flush()
            if last and score(last) > score(best or {}):
                best = last
        finally:
            ser.close()
        text = "".join(lines)
        readable = sum(c.isprintable() and c != "\ufffd" for c in text) / max(len(text), 1)   # � = undecodable byte
        if not lines:
            out(f"  · {baud} baud: nothing printed in {seconds:g} s")
            continue
        if readable < 0.9 and not any("rst:0x" in ln for ln in lines):     # reset noise is fine
            out(f"  · {baud} baud: unreadable (wrong speed)")
            continue
        out(f"  · {baud} baud, {len(lines)} lines, for example:")
        for ln in lines[-6:]:
            out(f"      {ln[:110]}")
        if reset and baud == 115200 and any("rst:0x" in ln or "boot:0x" in ln for ln in lines) \
                and not (best and score(best)):
            out("  · the USB link works (the ESP32 start-up message came through), but after it the sketch "
                "prints no readings on USB")
            for url in dict.fromkeys(re.findall(r"https?://\d+\.\d+\.\d+\.\d+(?::\d+)?[^\s\"']*", " ".join(lines))):
                out(f"  · the board says it serves {url}: reading it over Wi-Fi")
                if probe_fn(url, out=out):
                    return 0, None
            out("  · read the board over Wi-Fi instead (--find / --probe http://<board-ip>/)")
            return 0, None
        if any('"bme280"' in ln or '"scd40"' in ln or '"bno055"' in ln for ln in lines):
            out("  · this is the INTERNAL sensor board (BME280/SCD40/…), not the external one")
            return 0, None
        if best and score(best):
            out(f"  · recognised: {summarise(best)}")
            out(f"  ✓ use EXT_BOARD_PORT={port} EXT_BOARD_BAUD={baud}")
            return baud, best
        out("  ✗ readable, but no GNSS or Geiger values recognised in it")
        return 0, None
    if reset:
        out(f"  ✗ {port}: nothing readable at any usual speed, even after a restart. Either the board's program "
            "sends no text (or at an unusual speed), or the board isn't powered / the cable is charge-only. For the "
            "internal sensor board: flash the IMM-OS firmware (scripts/flash-esp32.sh), then listen again")
    else:
        out(f"  ✗ {port}: the board printed nothing readable. Run again with --reset to restart it and see "
            "whether the USB link works; its firmware may only serve Wi-Fi (use --find)")
    return 0, None


# ── Finding the board and its data URL ──────────────────────────────
_DATA_URL = re.compile(r"""(?:fetch|\$\.getJSON|\$\.get|\$\.ajax|axios\.get|EventSource)\s*\(\s*['"`]([^'"`]+)['"`]"""
                       r"""|\.open\s*\(\s*['"]GET['"]\s*,\s*['"`]([^'"`]+)['"`]""", re.I)
_WS = re.compile(r"""new\s+WebSocket\s*\(""", re.I)
COMMON_PATHS = ["/json", "/data", "/api", "/api/data", "/readings", "/sensors", "/sensor", "/values", "/status",
                "/getData", "/data.json", "/sensor-data"]


def summarise(line: dict) -> str:
    parts = [f"{sec}.{k}={v:g}" if isinstance(v, (int, float)) else f"{sec}.{k}={v}"
             for sec, vals in line.items() if isinstance(vals, dict) for k, v in vals.items()]
    return ", ".join(parts) or "nothing recognised"


def score(line: dict) -> int:
    return sum(len(v) for v in line.values() if isinstance(v, dict))


def probe(url: str, mapping=None, get=get_text, out=print) -> str:
    """Fetch url and show what is recognised; if it's a page that loads its data by script, try the
    data URLs it names and the usual ones. Returns the best URL for EXT_BOARD_URL ('' if none)."""
    from urllib.parse import urljoin
    try:
        body = get(url)
    except FETCH_ERRORS as e:
        out(f"  ✗ {url}: {e}")
        return ""
    line = recognise(parse_body(body), mapping)
    out(f"  · {url}: {summarise(line)}")
    best, best_score = (url, score(line)) if score(line) else ("", 0)
    if body.lstrip()[:1] not in "{[":
        named = [a or b for a, b in _DATA_URL.findall(body)]
        if named:
            out(f"  · the page loads data from: {', '.join(named)}")
        if _WS.search(body):
            out("  · the page also uses a WebSocket: values pushed that way aren't read; "
                "an HTTP data URL (below) or the IMM-OS firmware is needed")
        tried = set()
        for path in named + COMMON_PATHS:
            u = urljoin(url, path)
            if u in tried or u == url or u.startswith(("ws:", "wss:")):
                continue
            tried.add(u)
            try:
                b = get(u)
            except FETCH_ERRORS:
                continue
            ln = recognise(parse_body(b), mapping)
            if score(ln):
                out(f"  · {u}: {summarise(ln)}")
            if score(ln) > best_score:
                best, best_score = u, score(ln)
    if best:
        out(f"  ✓ use EXT_BOARD_URL={best}")
    else:
        out("  ✗ no GNSS or Geiger values found. Send the board's dashboard code to whoever maintains IMM-OS, "
            "set EXT_BOARD_MAP, or flash firmware/esp32-external")
    return best


def page_title(host, get=get_text) -> str:
    try:
        m = re.search(r"<title>(.*?)</title>", get(f"http://{host}/"), re.I | re.S)
        return f'"{m.group(1).strip()[:40]}"' if m else "no title"
    except FETCH_ERRORS:
        return "no answer"


def local_hosts():
    """Every address on this Pi's /24 networks (not itself)."""
    import ipaddress
    import subprocess
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/\d+", text)
    if not addrs:                     # Windows / macOS (no `ip`): the address this machine uses on the LAN
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect(("192.0.2.1", 9))          # no packet is sent; just picks the outgoing interface
                addrs = [u.getsockname()[0]]
        except OSError:
            addrs = []
    hosts = []
    for addr in addrs:
        net = ipaddress.ip_network(f"{addr}/24", strict=False)
        hosts += [str(h) for h in net.hosts() if str(h) != addr]
    return hosts


def find(hosts=None, get=get_text, out=print, port_open=None) -> list:
    """Look for a web server on the local networks whose data has GNSS or Geiger values."""
    def is_open(h):
        try:
            with socket.create_connection((h, 80), timeout=1.5):   # ESP32 Wi-Fi power save answers slowly
                return True
        except OSError:
            return False
    port_open = port_open or is_open
    hosts = local_hosts() if hosts is None else hosts
    out(f"  · looking at {len(hosts)} addresses for a web server …")
    with ThreadPoolExecutor(64) as ex:
        up = [h for h, o in zip(hosts, ex.map(port_open, hosts)) if o]
    found = []
    for h in up:
        try:
            best = probe(f"http://{h}/", get=get, out=lambda s: None)
        except Exception as e:                          # one odd device must not stop the search
            out(f"  · {h}: web server, unreadable ({type(e).__name__})")
            continue
        if best:
            out(f"  ✓ board at {h}: EXT_BOARD_URL={best}")
            found.append(best)
        else:
            out(f"  · {h}: web server, {page_title(h, get)}: no GNSS/Geiger values")
    if not found:
        out(f"  ✗ none of the {len(up)} web servers found serves GNSS or Geiger values "
            "(is the board on the same Wi-Fi as the Pi? try --probe http://<board-ip>/)")
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("stdout", "mqtt", "both"), default="stdout")
    ap.add_argument("--send", metavar="CMD", help="a command for the board over USB (STATUS, WIFI_SSID …)")
    ap.add_argument("--probe", nargs="?", const="", metavar="URL", help="show what the board serves (default EXT_BOARD_URL)")
    ap.add_argument("--find", action="store_true", help="look for the board on the local networks")
    ap.add_argument("--listen", nargs="?", const="", metavar="PORT",
                    help="show what a USB-connected board prints (default: every USB serial port)")
    ap.add_argument("--reset", action="store_true", help="with --listen: restart the ESP32 first, to see its start-up")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):           # Windows consoles: don't die on ✓ and ·
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    zone = os.getenv("EXT_BOARD_ZONE", "exterior")
    url, port = os.getenv("EXT_BOARD_URL", ""), os.getenv("EXT_BOARD_PORT", "")
    mapping = parse_map(os.getenv("EXT_BOARD_MAP", ""))
    baud = int(os.getenv("EXT_BOARD_BAUD", "115200") or 115200)
    if args.find:
        sys.exit(0 if find() else 1)
    if args.listen is not None:
        ports = [args.listen] if args.listen else serial_ports()
        if not ports:
            sys.exit("no USB serial device: plug the board into a Pi USB port with a data cable "
                     "(charge-only cables don't work); check with: ls /dev/ttyUSB* /dev/ttyACM*")
        ok = False
        for p in ports:
            print(f"── {p}")
            ok = listen(p, reset=args.reset)[0] > 0 or ok
        sys.exit(0 if ok else 1)
    if args.probe is not None:
        target = args.probe or url
        if not target:
            sys.exit("--probe needs a URL (or EXT_BOARD_URL): the address you open the board's dashboard with")
        if "<" in target or ">" in target:
            sys.exit("replace <board-ip> with the board's real address, e.g. --probe http://192.168.1.77/")
        if "://" not in target:
            target = "http://" + target
        sys.exit(0 if probe(target, mapping) else 1)
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
    from mqtt_publisher import make_publisher
    publish_fn = make_publisher(args.mode, f"habitat/sensors/geiger/{zone}")
    if url:
        run_http(url, publish_fn, zone, mapping=mapping)
    elif port:
        run_serial(port, publish_fn, zone, mapping=mapping, baud=baud)
    else:
        print(json.dumps({"error": "set EXT_BOARD_URL=http://<board-ip>/json (Wi-Fi) or EXT_BOARD_PORT (USB) "
                                   "in /etc/imm-os/edge.env"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
