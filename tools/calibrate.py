#!/usr/bin/env python3
"""
Manage this node's sensor calibration (/etc/imm-os/calibration.yaml).

Take raw readings with calibration switched off, compare against a reference
instrument, then record the correction:

  IMM_CALIBRATION_OFF=1 python3 sensor_drivers/bme280_driver.py      # raw values

  calibrate.py one-point bme280.temp --raw 23.6 --true 22.8 --reference "Testo 605i"
  calibrate.py two-point bme280.hum --raw 31.0,72.9 --true 33.0,75.3 --reference "salt test"
  calibrate.py set scd40.co2_ppm --offset 12 --reference "outdoor air 420 ppm"
  calibrate.py show | check | report | remove bme280.temp

one-point  keeps the existing gain and sets the offset so raw → true
two-point  fits gain and offset through both points (best for humidity, pH, O2)

Changes apply within one reading: drivers reload the file when it changes.
`report` prints a Markdown sign-off sheet for the acceptance checklist.
"""
import argparse
import os
import socket
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
from calibration import DEFAULT_PATH, CalibrationError, validate  # noqa: E402

HEADER = """# IMM-OS sensor calibration for this node — managed by tools/calibrate.py
# corrected = raw * gain + offset   (see core/calibration.py)
"""


def read(path: str) -> dict:
    import yaml
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return validate(yaml.safe_load(f)) or {}


def write(path: str, data: dict) -> None:
    """Validate, then replace the file atomically so a running driver never reads half a file."""
    import yaml
    validate(data)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".calibration-")
    with os.fdopen(fd, "w") as f:
        f.write(HEADER)
        yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False, allow_unicode=True)
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def target(spec: str):
    if spec.count(".") != 1:
        raise SystemExit(f"'{spec}': use SENSOR.FIELD, e.g. bme280.temp")
    return spec.split(".")


def floats(text: str, n: int):
    try:
        vals = [float(x) for x in text.split(",")]
    except ValueError:
        raise SystemExit(f"'{text}': expected {n} comma-separated numbers")
    if len(vals) != n:
        raise SystemExit(f"'{text}': expected {n} comma-separated numbers")
    return vals


def two_point(raw, true):
    (r1, r2), (t1, t2) = raw, true
    if abs(r2 - r1) < 1e-9:
        raise SystemExit("the two raw readings must differ (use two different reference points)")
    gain = (t2 - t1) / (r2 - r1)
    return round(gain, 6), round(t1 - gain * r1, 6)


def record(data: dict, spec: str, args, **math) -> dict:
    sensor, field = target(spec)
    entry = {k: v for k, v in math.items() if v is not None}
    entry.update(reference=args.reference, date=str(date.today()), by=args.by)
    if args.note:
        entry["note"] = args.note
    data.setdefault(sensor, {})[field] = entry
    return data


def report(data: dict) -> str:
    lines = [f"## Calibration sheet — {os.getenv('IMM_NODE_ID') or socket.gethostname()}", "",
             "| Sensor | Field | Gain | Offset | Reference | Date | By | Signed |",
             "|---|---|---|---|---|---|---|---|"]
    for sensor in sorted(data):
        for field, c in sorted(data[sensor].items()):
            lines.append(f"| {sensor} | {field} | {c.get('gain', 1)} | {c.get('offset', 0)} | "
                         f"{c.get('reference', '—')} | {c.get('date', '—')} | {c.get('by', '—')} | ☐ |")
    if len(lines) == 4:
        lines.append("| — | no calibrations recorded | | | | | | |")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default=os.getenv("IMM_CALIBRATION_FILE", DEFAULT_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_meta(sp):
        sp.add_argument("target", help="SENSOR.FIELD, e.g. bme280.temp")
        sp.add_argument("--reference", required=True, help="reference instrument / method")
        sp.add_argument("--by", default=os.getenv("SUDO_USER") or os.getenv("USER") or "unknown")
        sp.add_argument("--note")
        return sp

    s = with_meta(sub.add_parser("set", help="set gain and/or offset directly"))
    s.add_argument("--gain", type=float)
    s.add_argument("--offset", type=float)
    o = with_meta(sub.add_parser("one-point", help="offset from one raw/true pair"))
    o.add_argument("--raw", type=float, required=True)
    o.add_argument("--true", type=float, required=True)
    t = with_meta(sub.add_parser("two-point", help="gain + offset from two raw/true pairs"))
    t.add_argument("--raw", required=True, help="r1,r2")
    t.add_argument("--true", required=True, help="t1,t2")
    rm = sub.add_parser("remove", help="delete a correction")
    rm.add_argument("target")
    sub.add_parser("show")
    sub.add_parser("check")
    sub.add_parser("report")
    args = p.parse_args(argv)

    try:
        data = read(args.file)
    except CalibrationError as exc:
        raise SystemExit(f"{args.file} is invalid: {exc}")

    if args.cmd == "check":
        print(f"{args.file}: OK ({sum(len(v) for v in data.values())} corrections)")
        return
    if args.cmd == "show":
        import yaml
        print(yaml.safe_dump(data, sort_keys=True) if data else "(no corrections)")
        return
    if args.cmd == "report":
        print(report(data))
        return
    if args.cmd == "remove":
        sensor, field = target(args.target)
        if data.get(sensor, {}).pop(field, None) is None:
            raise SystemExit(f"{args.target}: no correction recorded")
        if not data[sensor]:
            del data[sensor]
        write(args.file, data)
        print(f"Removed {args.target}")
        return

    sensor, field = target(args.target)
    if args.cmd == "set":
        if args.gain is None and args.offset is None:
            raise SystemExit("give --gain and/or --offset")
        old = data.get(sensor, {}).get(field, {})
        gain = args.gain if args.gain is not None else old.get("gain")
        offset = args.offset if args.offset is not None else old.get("offset")
        record(data, args.target, args, gain=gain, offset=offset)
    elif args.cmd == "one-point":
        gain = data.get(sensor, {}).get(field, {}).get("gain", 1.0)
        offset = round(args.true - args.raw * gain, 6)
        record(data, args.target, args, gain=gain if gain != 1.0 else None, offset=offset)
    elif args.cmd == "two-point":
        raw, true = floats(args.raw, 2), floats(args.true, 2)
        gain, offset = two_point(raw, true)
        record(data, args.target, args, gain=gain, offset=offset)
        data[sensor][field].update(raw_points=raw, true_points=true)
    try:
        write(args.file, data)
    except CalibrationError as exc:
        raise SystemExit(f"Not saved: {exc}")
    c = data[sensor][field]
    off = c.get("offset", 0)
    print(f"{args.target}: corrected = raw × {c.get('gain', 1)} {'-' if off < 0 else '+'} {abs(off)}   → {args.file}")


if __name__ == "__main__":
    main()
