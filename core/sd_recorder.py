#!/usr/bin/env python3
"""
IMM-OS SD-card recorder: every reading this node publishes, as readable CSV on the Pi's SD card.

It listens on the node's own MQTT broker (localhost: every driver publishes there, see
tools/local_broker.py) and writes one CSV per sensor and zone:

    /var/lib/imm-os/records/<mission>-<id>/sol-01/bme280_zone_a.csv     during a mission
    /var/lib/imm-os/records/<mission>-<id>/pre-mission/2026-09-26/…     before its Sol 1
    /var/lib/imm-os/records/no-mission/2026-09-29/…                     otherwise (IST date)

Columns: time_ist, time_utc, node_id, zone, one per metric (fixed per sensor), extra (anything
else, as JSON). The sol comes from the mission start (T0), asked from the MCC
(/api/mission/clock) every 5 min and kept in records/.mission.json, so an MCC outage doesn't
stop the filing; a reading is filed by its own time, not when it arrived.

This is the node's second, human-readable copy of the mission (the blackbox is the encrypted
one, kept 9 days). Nothing is deleted unless IMM_RECORD_KEEP_DAYS is set; writing pauses when the
card has less than IMM_RECORD_MIN_FREE_MB left (default 1024), and resumes when space is freed.
About 60–100 MB a day for both sensor boards and node health.

Copy to the MCC PC (PowerShell):
    scp -r pratham@node-rpi-01.local:/var/lib/imm-os/records C:\\Users\\PRATHAM\\Documents\\pi-records

Environment: IMM_RECORD_DIR, IMM_RECORD_MIN_FREE_MB, IMM_RECORD_KEEP_DAYS, MQTT_HOST/PORT (the
local broker), MCC_HOST (for the mission clock), IMM_EDGE_CLIENT_SECRET (edge login).
"""
import csv
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchdog  # noqa: E402

log = logging.getLogger("sd_recorder")
IST = timezone(timedelta(hours=5, minutes=30))
SOL_S = 86400

# Fixed columns per sensor, so a metric that appears later (MQ-4 ch4_ppm after warm-up) still
# gets its own column; anything not listed goes into "extra".
COLUMNS = {
    "bme280": ["temp", "hum", "pres", "dew_point_c"],
    "scd40": ["co2_ppm", "temp", "hum", "dew_point_c", "asc"],
    "o2": ["o2_pct", "calibrated"],
    "bno055": ["heading_deg", "roll_deg", "pitch_deg", "lin_acc_ms2", "imu_calib", "grav_ms2", "mag_ut", "gyro_dps",
               "temp", "calib_gyro", "calib_acc", "calib_mag", "cal_restored"],
    "mq4": ["ch4_ppm", "rs_r0", "vout_mv", "rs_rl", "warming", "warm_left_s", "calibrated"],
    "board": ["uptime_s", "reset_reason", "boot_count", "i2c_err", "bme_resets", "rssi_dbm",
              "heal_cause", "heal_reboots", "heap_free", "heap_min", "wifi_drops", "wifi_reason", "net_restarts"],
    "geiger": ["cpm", "usv_h", "counts", "warming", "window_s"],
    "gnss": ["fix", "sats", "lat", "lon", "alt_m", "sog_kn", "cog_deg", "gnss_utc"],
    "sysmon": ["cpu_temp", "cpu_load", "mem_pct", "disk_pct", "fan_rpm", "power_w", "supply_v", "undervolt",
               "throttled", "undervolt_boot", "svc_failed", "svc_restarts", "mcc_link", "mqtt_backlog"],
    "mq7": ["co_ppm"], "tsl2561": ["lux"], "ina219": ["voltage_v", "current_ma", "power_mw"],
    "bms": ["battery_pct", "solar_w"], "max30100": ["hr_bpm", "spo2_pct"], "ecg_ad8232": ["voltage"],
}
META = {"sensor", "timestamp", "node_id", "zone", "crew_id", "simulated", "sig", "seq", "run", "q", "qf", "delayed"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "mission"


def folder_for(ts: float, mission: Optional[dict]) -> str:
    """Where a reading taken at ts is filed (relative to the records directory)."""
    day = datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")
    if not mission:
        return os.path.join("no-mission", day)
    base = f"{slug(mission['name'])}-{mission['id']}"
    start, sols = float(mission["start"]), int(mission["sols"])
    end = min(start + sols * SOL_S, float(mission["ended_at"])) if mission.get("ended_at") else start + sols * SOL_S
    if ts < start:
        return os.path.join(base, "pre-mission", day)
    if ts >= end:
        return os.path.join("no-mission", day)
    return os.path.join(base, f"sol-{int((ts - start) // SOL_S) + 1:02d}")


class Recorder:
    def __init__(self, root: str, min_free_mb: float = 1024, keep_days: float = 0,
                 disk_free=lambda p: shutil.disk_usage(p).free, now=time.time):
        self.root, self.min_free, self.keep_days = root, min_free_mb * 1e6, keep_days
        self.disk_free, self.now = disk_free, now
        self.files: Dict[str, dict] = {}
        self.mission: Optional[dict] = None
        self.paused = False
        self.lock = threading.Lock()
        self.written = 0
        os.makedirs(root, exist_ok=True)
        self._load_mission()
        readme = os.path.join(root, "README.txt")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write(__doc__.split("Environment:")[0].strip() + "\n")

    # ── mission (from the MCC, cached) ─────────────────────────────
    def _load_mission(self):
        try:
            with open(os.path.join(self.root, ".mission.json")) as f:
                self.mission = json.load(f).get("mission")
        except (OSError, ValueError):
            self.mission = None

    def set_mission(self, mission: Optional[dict]):
        if mission != self.mission:
            log.info("mission: %s", mission and f"{mission['name']} from {mission.get('start_ist')}")
        self.mission = mission
        tmp = os.path.join(self.root, ".mission.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"mission": mission, "saved": self.now()}, f)
        os.replace(tmp, os.path.join(self.root, ".mission.json"))

    # ── writing ────────────────────────────────────────────────────
    def record(self, topic: str, payload: dict) -> Optional[str]:
        """File one reading; → the CSV path it went to (None: not a reading, or paused)."""
        if not isinstance(payload, dict) or self.paused:
            return None
        parts = topic.split("/")
        sensor = str(payload.get("sensor") or (parts[2] if len(parts) > 2 else "unknown"))
        zone = str(payload.get("zone") or (parts[3] if len(parts) > 3 else "-"))
        ts = payload.get("timestamp")
        if not isinstance(ts, (int, float)):
            return None
        cols = COLUMNS.get(sensor)
        rel = os.path.join(folder_for(float(ts), self.mission), f"{re.sub(r'[^A-Za-z0-9_-]', '_', sensor)}_"
                                                             f"{re.sub(r'[^A-Za-z0-9_-]', '_', zone)}.csv")
        with self.lock:
            f = self.files.get(rel)
            if f is None:
                f = self._open(rel, sensor, payload, cols)
            values = {k: v for k, v in payload.items() if k not in META}
            row = [datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                   datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                   payload.get("node_id", ""), zone]
            row += ["" if values.get(c) is None else values.get(c) for c in f["columns"]]
            extra = {k: v for k, v in values.items() if k not in f["columns"]}
            row.append(json.dumps(extra, separators=(",", ":")) if extra else "")
            f["writer"].writerow(row)
            f["last"] = self.now()
            self.written += 1
        return os.path.join(self.root, rel)

    def _open(self, rel: str, sensor: str, payload: dict, cols) -> dict:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        if new:
            columns = list(cols or [k for k in payload if k not in META])
        else:                                          # appending after a restart: keep the file's header
            with open(path, newline="") as fh:
                header = next(csv.reader(fh), [])
            columns = header[4:-1]
        fh = open(path, "a", newline="")
        w = csv.writer(fh)
        if new:
            w.writerow(["time_ist", "time_utc", "node_id", "zone", *columns, "extra"])
        f = {"fh": fh, "writer": w, "columns": columns, "last": self.now()}
        self.files[rel] = f
        return f

    def flush(self, idle_close_s: float = 600) -> None:
        """Write buffered rows to the card (fsync), close files idle for 10 min, check free space."""
        with self.lock:
            for rel, f in list(self.files.items()):
                f["fh"].flush()
                try:
                    os.fsync(f["fh"].fileno())
                except OSError:
                    pass
                if self.now() - f["last"] > idle_close_s:
                    f["fh"].close()
                    del self.files[rel]
        free = self.disk_free(self.root)
        if not self.paused and free < self.min_free:
            self.paused = True
            log.error("SD card nearly full (%.0f MB free): recording paused until space is freed", free / 1e6)
        elif self.paused and free > self.min_free + 256e6:
            self.paused = False
            log.warning("free space back (%.0f MB): recording resumed", free / 1e6)
        if self.keep_days:
            self._prune()

    def _prune(self) -> None:
        cutoff = self.now() - self.keep_days * 86400
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            for name in filenames:
                p = os.path.join(dirpath, name)
                if name.endswith(".csv") and os.path.getmtime(p) < cutoff and \
                        os.path.relpath(p, self.root) not in self.files:
                    os.remove(p)
            if dirpath != self.root and not os.listdir(dirpath):
                os.rmdir(dirpath)

    def close(self) -> None:
        with self.lock:
            for f in self.files.values():
                f["fh"].close()
            self.files.clear()


# ── the MCC's mission clock ──────────────────────────────────────────
def fetch_mission(url: str) -> Optional[dict]:
    import requests
    from auth_client import auth_headers
    r = requests.get(url, headers=auth_headers(), timeout=10)
    r.raise_for_status()
    return r.json().get("mission")


def mission_loop(rec: Recorder, url: str, every_s: float = 300, fetch=fetch_mission, sleep=time.sleep, stop=None):
    while not (stop and stop.is_set()):
        try:
            rec.set_mission(fetch(url))
        except Exception as e:                      # MCC unreachable: keep the cached mission
            log.warning("mission clock from %s unavailable (%s): keeping %s", url, e,
                        rec.mission and rec.mission.get("name"))
        sleep(every_s)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import paho.mqtt.client as mqtt
    rec = Recorder(os.getenv("IMM_RECORD_DIR", "/var/lib/imm-os/records"),
                   float(os.getenv("IMM_RECORD_MIN_FREE_MB", "1024")), float(os.getenv("IMM_RECORD_KEEP_DAYS", "0")))
    url = os.getenv("IMM_MISSION_URL") or f"http://{os.getenv('MCC_HOST', 'imm.local')}/api/mission/clock"
    threading.Thread(target=mission_loop, args=(rec, url), daemon=True).start()

    def on_connect(client, *_):
        client.subscribe("habitat/sensors/#", qos=1)
        log.info("recording habitat/sensors/# from %s into %s", os.getenv("MQTT_HOST", "localhost"), rec.root)

    def on_message(_c, _u, msg):
        try:
            rec.record(msg.topic, json.loads(msg.payload))
        except (ValueError, OSError) as e:
            log.error("could not record %s: %s", msg.topic, e)

    client = mqtt.Client(client_id=f"imm-sd-recorder-{os.getenv('IMM_NODE_ID', 'node')}", clean_session=True)
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), 60)
    client.loop_start()
    last, quiet = rec.written, False
    try:
        while True:
            watchdog.kick()
            time.sleep(10)
            rec.flush()
            watchdog.status(f"{rec.written} readings recorded{' (PAUSED: card nearly full)' if rec.paused else ''}")
            if (rec.written == last) != quiet:              # say it when it changes, not every 10 s
                quiet = rec.written == last
                (log.warning if quiet else log.info)("no readings coming in (drivers stopped, or the local broker is down)"
                                                    if quiet else "readings coming in again")
            last = rec.written
    finally:
        rec.close()


if __name__ == "__main__":
    main()
