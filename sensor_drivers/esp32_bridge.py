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
    board   habitat/sensors/board/<zone>    uptime_s, reset_reason, boot_count, i2c_err, bme_resets, rssi_dbm,
                                            heal_cause, heal_reboots, heap_free, heap_min, wifi_drops,
                                            wifi_reason, net_restarts
                                            (every 10 s: the MCC alarms on crashes, watchdog
                                            resets, brownouts and BME280 power losses, and labels the
                                            board's own self-heal reboots with their cause)

dew_point_c is calculated here from temp and hum (Magnus formula). The same air has the same
dew point wherever it is measured, so the BME280's and SCD40's should agree even when their
temperatures differ (the SCD40 warms itself): a check that both humidity sensors are right.

Lines starting with '#' are the board's diagnostics (stderr as {"info": ...}).

  --send CMD    send a command to the board over USB and show its replies: STATUS, CAL_MQ4, CAL_O2,
                CAL_CO2 [ppm] (SCD40 forced recalibration in fresh air; self-calibration off),
                ASC_ON, CAL_BNO_CLEAR, WIFI_SSID <name>, WIFI_PASS <password>, WIFI_OFF.
                While the service runs, a reply can also land in its journal
                (journalctl -u imm-sensor-pipeline@esp32_bridge.py).

Over Wi-Fi: once the board has joined the network (WIFI_SSID / WIFI_PASS over USB; STATUS shows
its IP), set ESP32_URL=http://<board-ip>/json and the bridge polls it once a second instead of
reading USB. Commands (--send) still go over USB.
ESP32_URL=http://imm-sensors.local/json (the board's mDNS name) works on any router: the bridge
polls the IP the name resolves to, and keeps polling that IP if the name stops resolving (the
board's mDNS answer can go quiet while the board itself is fine), so that doesn't cost data.
Each failed poll is logged with what kind of failure it was (name, no answer, refused, no route),
which tells a board that is off the Wi-Fi from one whose web server or name stopped.
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
    "board": ("uptime_s", "reset_reason", "boot_count", "i2c_err", "bme_resets", "rssi_dbm",
              "heal_cause", "heal_reboots", "heap_free", "heap_min", "wifi_drops", "wifi_reason", "net_restarts"),
}
# both links (ESP32_URL and ESP32_PORT set): every reading says which one brought it ("via"), and the
# board's health reading says which are delivering (usb_link, wifi_link: 1 or 0)
INT_FIELDS = {"imu_calib", "calib_gyro", "calib_acc", "calib_mag", "warming", "calibrated", "warm_left_s", "cal_restored", "asc",
              "uptime_s", "reset_reason", "boot_count", "i2c_err", "bme_resets", "rssi_dbm",
              "heal_cause", "heal_reboots", "heap_free", "heap_min", "wifi_drops", "wifi_reason", "net_restarts"}
HEAL_CAUSES = {1: "I2C bus stall", 2: "Wi-Fi lost", 3: "not polled", 4: "memory low"}
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


def explain_mq4(value: dict, warned: set) -> None:
    mq4 = value.get("mq4") if isinstance(value.get("mq4"), dict) else {}
    state = "warming" if mq4.get("warming") else ("uncalibrated" if mq4 and not mq4.get("calibrated") else None)
    if state and state not in warned:
        warned.add(state)
        msg = ("MQ-4 warming up (3 min): no ch4_ppm yet" if state == "warming" else
               "MQ-4 not calibrated: send CAL_MQ4 in clean air (esp32_bridge.py --send CAL_MQ4)")
        print(json.dumps({"info": msg}), file=sys.stderr, flush=True)


class BoardAddress:
    """http://imm-sensors.local/json → the URL to poll: by the IP the name last resolved to.

    The name is looked up again every RESOLVE_EVERY_S and after a failed poll. If it stops
    resolving (the board's mDNS answer went quiet, or the Pi's avahi lost it) the last IP is kept:
    the board almost always still has it, so the data keeps flowing instead of stopping until
    someone presses RESET. An IP in the URL is used as it is."""

    RESOLVE_EVERY_S = 60.0
    RETRY_NAME_S = 10.0             # while the name doesn't resolve: don't wait on avahi every poll

    def __init__(self, url, resolve=None, clock=time.monotonic):
        import ipaddress
        from urllib.parse import urlsplit
        self.url, self.clock = url, clock
        u = urlsplit(url)
        self.host, self.port = u.hostname or "", u.port
        try:
            ipaddress.ip_address(self.host)
            self.by_name = False
        except ValueError:
            self.by_name = bool(self.host)
        self.resolve = resolve or self._getaddrinfo
        self.ip, self.next_lookup, self.name_ok = None, 0.0, True

    def _getaddrinfo(self, host):
        import socket
        return socket.getaddrinfo(host, self.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]

    def target(self) -> str:
        if not self.by_name:
            return self.url
        if self.ip is None or self.clock() >= self.next_lookup:
            self._lookup()
        if self.ip is None:
            return self.url
        netloc = self.ip + (f":{self.port}" if self.port else "")
        return self.url.replace(self.host + (f":{self.port}" if self.port else ""), netloc, 1)

    def _lookup(self):
        try:
            ip = self.resolve(self.host)
        except OSError as e:                     # socket.gaierror: the name doesn't resolve
            if self.name_ok:
                msg = (f"{self.host} stopped resolving ({e}); polling its last IP {self.ip}" if self.ip
                       else f"{self.host} doesn't resolve ({e})")
                print(json.dumps({"error": msg}), file=sys.stderr, flush=True)
            self.name_ok = False
            self.next_lookup = self.clock() + self.RETRY_NAME_S
            return
        if self.ip and ip != self.ip:
            print(json.dumps({"info": f"{self.host} is now at {ip} (was {self.ip})"}), file=sys.stderr, flush=True)
        elif not self.name_ok:
            print(json.dumps({"info": f"{self.host} resolves again ({ip})"}), file=sys.stderr, flush=True)
        self.ip, self.name_ok = ip, True
        self.next_lookup = self.clock() + self.RESOLVE_EVERY_S

    def failed(self):
        """A poll failed: look the name up again before the next one (the IP may have changed)."""
        if self.name_ok:
            self.next_lookup = 0.0


def failure_kind(e) -> str:
    """What a failed poll means, in words: the journal then says which fault it was."""
    import errno
    import socket
    inner = getattr(e, "reason", e)              # urllib wraps the socket error
    text = str(e).lower()
    if isinstance(inner, socket.gaierror) or "name or service not known" in text or "name resolution" in text:
        return "name not resolving (mDNS)"
    if isinstance(inner, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "no answer (timed out): board off the Wi-Fi, without power, or frozen"
    if isinstance(inner, ConnectionRefusedError) or "refused" in text:
        return "refused: board on the network, its web server down"
    if getattr(inner, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH) or "no route" in text:
        return "no route to host: board not on the Wi-Fi"
    return "error"


def note_restart(line: dict, boot):
    """Say in the journal when the board's boot counter changes (and why, if it rebooted itself)."""
    b = line.get("board")
    if isinstance(b, dict) and isinstance(b.get("boot_count"), int):
        if boot is not None and b["boot_count"] != boot:
            cause = HEAL_CAUSES.get(b.get("heal_cause") or 0)
            print(json.dumps({"info": f"ESP32 board restarted (boot {b['boot_count']}, reset reason "
                                      f"{b.get('reset_reason')}" + (f", self-heal: {cause}" if cause else "")
                                      + ")"}), file=sys.stderr, flush=True)
        return b["boot_count"]
    return boot


def run_http(url, publish_fn, now=time.time, sleep=time.sleep, fetch=None, max_loops=None, address=None,
             clock=time.monotonic):
    """Poll the board's /json over Wi-Fi once a second (the same line it prints on USB)."""
    import urllib.error
    from external_board_bridge import FETCH_ERRORS, Dedup, poll_http
    import watchdog
    fetch = fetch or poll_http
    address = address or BoardAddress(url, clock=clock)
    dedup, warned, failing, n, down_since, boot = Dedup(), set(), 0, 0, None, None
    while max_loops is None or n < max_loops:
        n += 1
        watchdog.kick()                 # alive while the board is away (reported, not restarted)
        target = address.target()
        try:
            line = fetch(target)
            if failing:
                print(json.dumps({"info": f"ESP32 board reachable again after {failing} failed poll(s), "
                                          f"{clock() - down_since:.0f} s"}), file=sys.stderr, flush=True)
            failing = 0
            if isinstance(line, dict) and dedup.new(line):
                boot = note_restart(line, boot)
                for topic, payload in to_payloads(line, now()):
                    publish_fn(payload, topic)
                explain_mq4(line, warned)
        except FETCH_ERRORS + (urllib.error.URLError,) as e:
            failing += 1
            if failing == 1:
                down_since = clock()
            address.failed()
            if failing in (1, 10) or failing % 60 == 0:
                print(json.dumps({"error": f"ESP32 board not answering at {target}: {failure_kind(e)} ({e})"}),
                      file=sys.stderr, flush=True)
        sleep(1.0)


# ── both links at once: USB cable and Wi-Fi (core/dual_link.py) ─────────
USB_HOST_EVERY_S = 20.0     # tell the board the Pi reads its USB: then a Wi-Fi outage never reboots it


def _log(msg: dict) -> None:
    print(json.dumps(msg), file=sys.stderr, flush=True)


def usb_link(link, port, opener=None, heartbeat_s=USB_HOST_EVERY_S, clock=time.monotonic):
    """Thread: the board's lines from its USB cable → link (reopening the port if it goes away)."""
    opener = opener or open_port
    ser, next_beat, failing, last_boot, old_firmware = None, 0.0, 0, None, False
    while not link.stop.is_set():
        if ser is None:
            try:
                ser = opener(port)
                ser.reset_input_buffer()
                if failing:
                    _log({"info": f"ESP32 board: USB port {port} open again"})
                failing = 0
            except Exception as e:                           # unplugged, or another program holds it
                failing += 1
                if failing in (1, 10) or failing % 60 == 0:
                    _log({"error": f"ESP32 board: USB port {port}: {e}"})
                link.stop.wait(5.0)
                continue
        try:
            if clock() >= next_beat:
                ser.write(b"USB_HOST\n")
                next_beat = clock() + heartbeat_s
            raw = ser.readline()
        except Exception as e:
            _log({"error": f"ESP32 board: USB read failed ({e}); reopening {port}"})
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            continue
        parsed = parse_line(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "data":
            link.put("usb", value)
        elif kind == "info":
            if value.startswith("unknown command"):          # firmware older than USB_HOST: say it once
                if not old_firmware:
                    _log({"info": "ESP32 board firmware predates USB_HOST: flash the current firmware, or a "
                                  "Wi-Fi outage can still make it reboot (a few seconds of USB data)"})
                old_firmware = True
            else:
                _log({"info": f"esp32: {value}"})
        elif kind == "boot":
            if value != last_boot:
                _log({"info": f"esp32 start-up: {value[:120]}"})
            last_boot = value


def wifi_link(link, url, fetch=None, address=None, wait=None):
    """Thread: poll the board's /json over Wi-Fi once a second → link."""
    import urllib.error
    from external_board_bridge import FETCH_ERRORS, poll_http
    fetch = fetch or poll_http
    address = address or BoardAddress(url)
    wait = wait or link.stop.wait
    failing = 0
    while not link.stop.is_set():
        target = address.target()
        try:
            line = fetch(target)
            if failing:
                _log({"info": f"ESP32 board answering over Wi-Fi again after {failing} failed poll(s)"})
            failing = 0
            if isinstance(line, dict):
                link.put("wifi", line)
        except FETCH_ERRORS + (urllib.error.URLError,) as e:
            failing += 1
            address.failed()
            if failing in (1, 10) or failing % 60 == 0:
                _log({"error": f"ESP32 board not answering at {target}: {failure_kind(e)} ({e})"})
        wait(1.0)


def run_dual(url, port, publish_fn, now=time.time, link=None, readers=None, max_items=None):
    """Read the board over its USB cable and over Wi-Fi at once; publish every reading once.

    Each line carries the board's "ms" counter, so the copy that comes over the second link is
    dropped and a reading one link missed is still taken from the other (core/dual_link.py)."""
    import queue
    import watchdog
    from dual_link import DualLink, LinkReport
    link = link or DualLink()
    for target, arg in (readers if readers is not None else [(usb_link, port), (wifi_link, url)]):
        link.run(target, arg)
    report = LinkReport("ESP32 board", _log)
    warned, boot, n = set(), None, 0
    try:
        while max_items is None or n < max_items:
            watchdog.kick()
            try:
                via, line = link.queue.get(timeout=1.0)
            except queue.Empty:
                report.update(link.links())
                continue
            n += 1
            ms = line.get("ms")
            if not link.accept(via, ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None):
                continue
            report.update(link.links())
            boot = note_restart(line, boot)
            for topic, payload in to_payloads(line, now()):
                payload["via"] = via
                if payload["sensor"] == "board":
                    payload.update(link.links())
                publish_fn(payload, topic)
            explain_mq4(line, warned)
    finally:
        link.stop.set()


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
            explain_mq4(value, warned)
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
    word, _, rest = command.strip().partition(" ")
    ser.write((word.upper() + (" " + rest if rest else "")).encode() + b"\n")   # Wi-Fi name / password keep their case
    deadline = time.monotonic() + listen_s
    got = False
    while time.monotonic() < deadline:
        try:
            raw = ser.readline()
        except Exception as e:                   # the board reset, or another program read the same bytes
            print(f"  (USB read interrupted: {e}; still listening)")
            time.sleep(0.3)
            continue
        parsed = parse_line(raw.decode("utf-8", "replace"))
        if parsed and parsed[0] == "info":
            print("  esp32:", parsed[1])
            got = True
    if not got:
        print(f"  no reply from the board on {getattr(ser, 'port', 'USB')} in {listen_s:.0f} s: it may be restarting (send the "
              "command again in 10 s), or this is not the IMM-OS firmware (flash it: scripts/flash-esp32.sh)")
    return 0 if got else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--send", metavar="CMD",
                        help="STATUS, CAL_MQ4, CAL_O2, CAL_CO2 [ppm], ASC_ON, SCD_TEST, SCD_RESET, SCD_OFF, SCD_ON, CAL_BNO_CLEAR, WIFI_SSID <name>, WIFI_PASS <pw>, WIFI_OFF")
    args = parser.parse_args()
    url = os.getenv("ESP32_URL", "").strip()
    pinned = os.getenv("ESP32_PORT", "").strip()
    if url and pinned and not args.send:          # both links: USB cable and Wi-Fi
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        run_dual(url, pinned, make_publisher(args.mode, "habitat/sensors/esp32/zone1"))
        return
    if url and not args.send:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        run_http(url, make_publisher(args.mode, "habitat/sensors/esp32/zone1"))
        return
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
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from external_board_bridge import port_users
            others = port_users(port)
        except Exception:
            others = []
        if others:
            print("  note: another program has this port open, so some replies may go to it (its journal):")
            for o in others:
                print("   ", o)
            print("  for clean replies: sudo systemctl stop imm-sensor-pipeline@esp32_bridge.py  (start it again after)")
        long = args.send.strip().upper().startswith("SCD_TEST")      # the self-test itself takes 10 s
        sys.exit(send(ser, args.send, listen_s=20.0 if long else 13.0))
    read_loop(ser, make_publisher(args.mode, "habitat/sensors/esp32/zone1"))


if __name__ == "__main__":
    main()
