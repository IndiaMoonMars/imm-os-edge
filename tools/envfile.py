#!/usr/bin/env python3
"""
Set keys in a KEY=VALUE env file (e.g. /etc/imm-os/edge.env) in place.

Existing keys are updated where they are; new keys are appended; comments and
other lines are kept. Values containing spaces, ';', '#', quotes or '$' are
double-quoted so the file works both as a systemd EnvironmentFile and when
sourced by bash.

  envfile.py /etc/imm-os/edge.env IMM_NODE_ID=node-rpi-01 MQTT_PORT=8883
  envfile.py --get /etc/imm-os/edge.env MQTT_HOST
"""
import os
import re
import sys
import tempfile

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_NEEDS_QUOTES = re.compile(r"[\s;#'\"$`\\|&<>()]")


def quote(value: str) -> str:
    if value == "" or not _NEEDS_QUOTES.search(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        return re.sub(r'\\(["\\$`])', r"\1", inner) if raw[0] == '"' else inner
    return raw


def get(lines, key):
    value = None
    for line in lines:
        m = _KEY.match(line)
        if m and m.group(1) == key:
            value = unquote(line.split("=", 1)[1])
    return value


def set_keys(lines, updates: dict):
    out, done = [], set()
    for line in lines:
        m = _KEY.match(line)
        if m and m.group(1) in updates:
            key = m.group(1)
            if key in done:          # drop later duplicates so the file has one value
                continue
            out.append(f"{key}={quote(updates[key])}")
            done.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in done:
            out.append(f"{key}={quote(value)}")
    return out


def main(argv):
    if len(argv) >= 3 and argv[0] == "--get":
        path, key = argv[1], argv[2]
        lines = open(path).read().splitlines() if os.path.exists(path) else []
        value = get(lines, key)
        if value is None:
            return 1
        print(value)
        return 0
    if len(argv) < 2 or any("=" not in a for a in argv[1:]):
        print(__doc__, file=sys.stderr)
        return 2
    path = argv[0]
    updates = dict(a.split("=", 1) for a in argv[1:])
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    new = set_keys(lines, updates)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".env-")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(new) + "\n")
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
