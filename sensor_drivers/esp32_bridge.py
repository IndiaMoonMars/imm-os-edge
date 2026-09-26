#!/usr/bin/env python3
"""
IMM-OS ESP32 sensor-board bridge: the board's JSON lines (USB serial) → one stream per sensor.

The ESP32 (firmware/esp32-sensors) sends one line per second, e.g.
    {"ms":1000,"bme280":{"temp":24.5,"hum":41.2,"pres":1008.4},"o2":{"o2_pct":20.9},...}
and each section is published on its own topic in the same shape as the Pi-wired drivers:

    bme280  habitat/sensors/bme280/<zone>   temp, hum, pres
    scd40   habitat/sensors/scd40/<zone>    co2_ppm, temp, hum
    o2      habitat/sensors/o2/<zone>       o2_pct            (DFRobot SEN0322)
    bno055  habitat/sensors/bno055/<zone>   heading_deg, roll_deg, pitch_deg, lin_acc_ms2, imu_calib
    mq4     habitat/sensors/mq4/<zone>      vout_mv, rs_r0, ch4_ppm (the last two once calibrated/warm)

Lines starting with '#' are the board's diagnostics (stderr as {"info": ...}).

  --send CMD    send STATUS, CAL_MQ4 or CAL_O2 to the board and show its replies
Port: ESP32_PORT, else the first CP210x/CH340 USB-serial device. Modes: stdout | mqtt | both
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from hw import esp32_port  # noqa: E402
from mqtt_publisher import MODES, make_publisher  # noqa: E402

BAUD_RATE = 115200
FIELDS = {
    "bme280": ("temp", "hum", "pres"),
    "scd40": ("co2_ppm", "temp", "hum"),
    "o2": ("o2_pct",),
    "bno055": ("heading_deg", "roll_deg", "pitch_deg", "lin_acc_ms2", "imu_calib"),
    "mq4": ("vout_mv", "rs_r0", "ch4_ppm"),
}
INT_FIELDS = {"imu_calib"}


def parse_line(line: str):
    """('data', dict) | ('info', text) | ('bad', text) | None for a blank line."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("#"):
        return "info", line.lstrip("# ")
    try:
        data = json.loads(line)
    except ValueError:
        return "bad", line
    return ("data", data) if isinstance(data, dict) else ("bad", line)


def to_payloads(data: dict, now: float):
    """One board line → [(topic, payload)]; unknown sections and non-numeric values are dropped."""
    out = []
    for sensor, fields in FIELDS.items():
        section = data.get(sensor)
        if not isinstance(section, dict):
            continue
        payload = {"sensor": sensor, "timestamp": round(now, 3)}
        for f in fields:
            v = section.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                payload[f] = int(v) if f in INT_FIELDS else float(v)
        if len(payload) > 2:
            out.append((f"habitat/sensors/{sensor}/zone1", payload))
    return out


def open_port(port: str):
    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, BAUD_RATE, 2.0
    ser.dtr = ser.rts = False          # don't pulse EN/IO0: opening the port must not reset the board
    ser.open()
    return ser


def read_loop(ser, publish_fn, now=time.time):
    ser.reset_input_buffer()
    warned = set()
    while True:
        try:
            parsed = parse_line(ser.readline().decode("utf-8", "replace"))
        except Exception as e:
            print(json.dumps({"error": f"USB serial read: {e}"}), file=sys.stderr, flush=True)
            time.sleep(1.0)
            continue
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "data":
            for topic, payload in to_payloads(value, now()):
                publish_fn(payload, topic)
            mq4 = value.get("mq4") if isinstance(value.get("mq4"), dict) else {}
            state = "warming" if mq4.get("warming") else ("uncalibrated" if mq4 and not mq4.get("calibrated") else None)
            if state and state not in warned:
                warned.add(state)
                msg = ("MQ-4 warming up (3 min): no ch4_ppm yet" if state == "warming" else
                       "MQ-4 not calibrated: send CAL_MQ4 in clean air (esp32_bridge.py --send CAL_MQ4)")
                print(json.dumps({"info": msg}), file=sys.stderr, flush=True)
        elif kind == "info":
            print(json.dumps({"info": f"esp32: {value}"}), file=sys.stderr, flush=True)
        else:
            print(json.dumps({"error": f"Invalid ESP32 line: {value[:120]!r}"}), file=sys.stderr, flush=True)


def send(ser, command: str, listen_s: float = 13.0) -> int:
    ser.reset_input_buffer()
    ser.write(command.strip().upper().encode() + b"\n")
    deadline = time.monotonic() + listen_s
    got = False
    while time.monotonic() < deadline:
        parsed = parse_line(ser.readline().decode("utf-8", "replace"))
        if parsed and parsed[0] == "info":
            print("  esp32:", parsed[1])
            got = True
    return 0 if got else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--send", metavar="CMD", help="STATUS, CAL_MQ4 or CAL_O2")
    args = parser.parse_args()
    port = esp32_port()
    if not port:
        print(json.dumps({"error": "no ESP32 found on USB (plug the board into the Pi; or set ESP32_PORT)"}),
              file=sys.stderr)
        sys.exit(1)
    try:
        ser = open_port(port)
    except Exception as e:
        print(json.dumps({"error": f"USB serial {port}: {e}"}), file=sys.stderr)
        sys.exit(1)
    if args.send:
        sys.exit(send(ser, args.send))
    read_loop(ser, make_publisher(args.mode, "habitat/sensors/esp32/zone1"))


if __name__ == "__main__":
    main()
