#!/usr/bin/env python3
"""
IMM-OS ESP32 sensor-board bridge: the board's JSON lines (USB serial) → one stream per sensor.

The ESP32 (firmware/esp32-sensors) sends one line per second, e.g.
    {"ms":1000,"bme280":{"temp":24.5,"hum":41.2,"pres":1008.4},"o2":{"o2_pct":20.9},...}
and each section is published on its own topic in the same shape as the Pi-wired drivers:

    bme280  habitat/sensors/bme280/<zone>   temp, hum, pres, dew_point_c
    scd40   habitat/sensors/scd40/<zone>    co2_ppm, temp, hum, dew_point_c
    o2      habitat/sensors/o2/<zone>       o2_pct, calibrated (DFRobot SEN0322; calibrated 0 until CAL_O2)
    bno055  habitat/sensors/bno055/<zone>   heading_deg, roll_deg, pitch_deg, lin_acc_ms2, imu_calib,
                                            grav_ms2, mag_ut, gyro_dps, temp, calib_gyro/acc/mag
    mq4     habitat/sensors/mq4/<zone>      vout_mv, rs_rl, rs_r0, ch4_ppm, warming, calibrated
                                            (rs_r0 once calibrated, ch4_ppm once also warm)
    board   habitat/sensors/board/<zone>    uptime_s, reset_reason, boot_count, i2c_err, bme_resets
                                            (every 10 s: the MCC alarms on crashes, watchdog
                                            resets, brownouts and BME280 power losses)

dew_point_c is calculated here from temp and hum (Magnus formula). The same air has the same
dew point wherever it is measured, so the BME280's and SCD40's should agree even when their
temperatures differ (the SCD40 warms itself): a check that both humidity sensors are right.

Lines starting with '#' are the board's diagnostics (stderr as {"info": ...}).

  --send CMD    send a command to the board and show its replies: STATUS, CAL_MQ4, CAL_O2,
                CAL_CO2 [ppm] (SCD40 forced recalibration in fresh air; self-calibration off),
                ASC_ON, CAL_BNO_CLEAR. While the service runs, a reply can also land in its journal
                (journalctl -u imm-sensor-pipeline@esp32_bridge.py).
Port: ESP32_PORT, else the first CP210x/CH340 USB-serial device. Modes: stdout | mqtt | both
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from hw import esp32_port  # noqa: E402
from mqtt_publisher import MODES, make_publisher  # noqa: E402

BAUD_RATE = 115200
FIELDS = {
    "bme280": ("temp", "hum", "pres"),
    "scd40": ("co2_ppm", "temp", "hum", "asc"),
    "o2": ("o2_pct", "calibrated"),
    "bno055": ("heading_deg", "roll_deg", "pitch_deg", "lin_acc_ms2", "imu_calib",
               "grav_ms2", "mag_ut", "gyro_dps", "temp", "calib_gyro", "calib_acc", "calib_mag", "cal_restored"),
    "mq4": ("vout_mv", "rs_rl", "rs_r0", "ch4_ppm", "warming", "calibrated", "warm_left_s"),
    "board": ("uptime_s", "reset_reason", "boot_count", "i2c_err", "bme_resets"),
}
INT_FIELDS = {"imu_calib", "calib_gyro", "calib_acc", "calib_mag", "warming", "calibrated", "warm_left_s", "cal_restored", "asc",
              "uptime_s", "reset_reason", "boot_count", "i2c_err", "bme_resets"}
DEW_POINT_SENSORS = ("bme280", "scd40")


def dew_point(temp_c: float, rh_pct: float):
    """Magnus formula (Sonntag 1990 constants), ±0.35 °C for -45…60 °C; None for RH 0."""
    if rh_pct <= 0:
        return None
    g = math.log(min(rh_pct, 100.0) / 100.0) + 17.62 * temp_c / (243.12 + temp_c)
    return 243.12 * g / (17.62 - g)


def parse_line(line: str):
    """('data', dict) | ('info', text) | ('boot', text) | ('bad', text) | None for a blank line.

    'boot': not ours, e.g. the ESP32 ROM's reset banner ("rst:0x1 (POWERON_RESET)…") or an
    ESP-IDF log line; 'bad': a data line that doesn't parse (cut off, garbled)."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("#"):
        return "info", line.lstrip("# ")
    if not line.startswith("{"):
        return "boot", line
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
        if sensor in DEW_POINT_SENSORS and "temp" in payload and "hum" in payload:
            dp = dew_point(payload["temp"], payload["hum"])
            if dp is not None:
                payload["dew_point_c"] = round(dp, 2)
        if len(payload) > 2:
            out.append((f"habitat/sensors/{sensor}/zone1", payload))
    return out


def open_port(port: str):
    import serial
    # Opening the port must not reset the board (that restarts the MQ-4 warm-up and loses the
    # BNO055 calibration). The DevKit's auto-reset circuit pulls EN low only while RTS is on and
    # DTR is off. Linux turns both on when the port opens, which is harmless; turning DTR off
    # first (as setting both False before open() does) passes through that reset state, so RTS
    # goes off first (IO0 low while EN stays high: nothing happens), then DTR.
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, BAUD_RATE, 2.0
    ser.open()
    ser.rts = False
    ser.dtr = False
    return ser


def read_loop(ser, publish_fn, now=time.time):
    ser.reset_input_buffer()
    warned = set()
    last_boot = None
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
        elif kind == "boot":
            if value != last_boot:                         # the ROM repeats its banner: say it once
                print(json.dumps({"info": f"esp32 start-up: {value[:120]}"}), file=sys.stderr, flush=True)
            last_boot = value
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
    parser.add_argument("--send", metavar="CMD", help="STATUS, CAL_MQ4, CAL_O2, CAL_CO2 [ppm], ASC_ON, CAL_BNO_CLEAR")
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
