#!/usr/bin/env python3
"""
IMM-OS node health driver — runs on every edge node (Raspberry Pi 4/5, any Linux board).

Publishes on habitat/sensors/sysmon/<zone> every --interval seconds:
  cpu_temp        °C, SoC temperature (thermal_zone0)
  cpu_load        %, CPU busy since the previous reading
  mem_pct         %, memory in use (MemAvailable-based)
  disk_pct        %, root filesystem used
  fan_rpm         Pi 5 Active Cooler / case fan, when present
  power_w         W, Pi 5 only: sum of V×I over the PMIC rails (`vcgencmd pmic_read_adc`).
                  This is the board's own draw; USB/HAT loads on the 5 V rail are not included.
  supply_v        V, Pi 5 only: 5 V input as measured by the PMIC (EXT5V_V)
  undervolt       1 while the supply is below ~4.63 V (`vcgencmd get_throttled` bit 0)
  throttled       1 while the CPU is throttled or frequency-capped (bits 1, 2)
  undervolt_boot  1 if under-voltage happened at any time since boot (bit 16)
  svc_failed      IMM-OS services (imm-*.service) currently failed
  svc_restarts    automatic restarts of IMM-OS services since they were started (watchdog,
                  crashes): the MCC raises an alarm when it keeps climbing
  mcc_link        1 while the local broker's bridge to the MCC is up, 0 while it queues
  mqtt_backlog    readings queued in the local broker for the MCC

A Pi 5 needs the 27 W (5 V / 5 A) supply: on a 3 A supply undervolt/undervolt_boot
flag it and USB current is limited. vcgencmd needs the service user in the `video` group.

Modes: stdout | mqtt | both
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from mqtt_publisher import MODES, make_publisher  # noqa: E402

MQTT_TOPIC = "habitat/sensors/sysmon/node"
SYS = os.getenv("SYSMON_ROOT", "")   # prefix for /sys and /proc (tests)

_RAIL = re.compile(r"^\s*(\S+)_([AV])\s+(?:current|volt)\(\d+\)=([0-9.]+)[AV]\s*$")


def _read(path: str) -> str:
    with open(SYS + path) as f:
        return f.read()


def cpu_temp():
    try:
        return round(int(_read("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def cpu_times():
    """(busy, total) jiffies from the aggregate cpu line of /proc/stat."""
    try:
        fields = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
    return sum(fields) - idle, sum(fields)


def cpu_load(prev, now):
    if not prev or not now or now[1] <= prev[1]:
        return None
    return round(100.0 * (now[0] - prev[0]) / (now[1] - prev[1]), 1)


def mem_pct():
    try:
        info = {}
        for line in _read("/proc/meminfo").splitlines():
            key, rest = line.split(":", 1)
            info[key] = int(rest.split()[0])
        return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return None


def disk_pct(path: str = "/"):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        return round(100.0 * (total - st.f_bavail * st.f_frsize) / total, 1) if total else None
    except OSError:
        return None


def fan_rpm():
    for path in sorted(glob.glob(SYS + "/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input")):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def vcgencmd(*args):
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=5, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None


def parse_pmic(text: str):
    """`vcgencmd pmic_read_adc` output → (total W over rails with both V and I, EXT5V volts)."""
    amps, volts = {}, {}
    for line in (text or "").splitlines():
        m = _RAIL.match(line)
        if m:
            (amps if m.group(2) == "A" else volts)[m.group(1)] = float(m.group(3))
    rails = set(amps) & set(volts)
    power = round(sum(amps[r] * volts[r] for r in rails), 2) if rails else None
    return power, volts.get("EXT5V")


def parse_throttled(text: str):
    """`throttled=0x50005` → {undervolt, throttled, undervolt_boot} as 0/1."""
    m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", text or "")
    if not m:
        return {}
    bits = int(m.group(1), 0)
    return {"undervolt": bits & 0x1, "throttled": int(bool(bits & 0x6)), "undervolt_boot": int(bool(bits & 0x10000))}


SKIP_UNITS = {"imm-mcc-discovery.service"}   # a periodic check; its failure means "MCC not found", not a crash


def parse_units(show_output: str):
    """`systemctl show -p Id -p ActiveState -p NRestarts <units>` → (failed, restarts)."""
    failed = restarts = 0
    for block in show_output.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not props.get("Id", "").endswith(".service") or props.get("Id") in SKIP_UNITS:
            continue
        failed += props.get("ActiveState") == "failed"
        try:
            restarts += int(props.get("NRestarts") or 0)
        except ValueError:
            pass
    return failed, restarts


def service_health():
    if not shutil.which("systemctl"):
        return {}
    try:
        units = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "imm-*"],
                               capture_output=True, text=True, timeout=10).stdout.split()
        units = [u for u in units if u.startswith("imm-") and u.endswith(".service")]
        if not units:
            return {}
        out = subprocess.run(["systemctl", "show", "-p", "Id", "-p", "ActiveState", "-p", "NRestarts", *units],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    failed, restarts = parse_units(out)
    return {"svc_failed": failed, "svc_restarts": restarts}


class BrokerStatus:
    """The local broker's link to the MCC and its backlog (store-and-forward nodes only)."""

    def __init__(self, node: str):
        import paho.mqtt.client as mqtt
        self.state_topic = f"imm/bridge/{node}/state"
        self.values = {}
        c = mqtt.Client(client_id=f"imm-sysmon-{node}")
        c.on_connect = lambda cl, u, f, rc: cl.subscribe(
            [(self.state_topic, 0), ("$SYS/broker/store/messages/count", 0), ("$SYS/broker/retained messages/count", 0)])
        c.on_message = self._on_message
        c.connect_async("127.0.0.1", int(os.getenv("IMM_LOCAL_BROKER_PORT", "1883")), 30)
        c.loop_start()
        self.client = c

    def _on_message(self, client, userdata, msg):
        try:
            self.values[msg.topic] = int(msg.payload.decode().strip())
        except ValueError:
            pass

    def read(self) -> dict:
        v = self.values
        out = {}
        if self.state_topic in v:
            out["mcc_link"] = 1 if v[self.state_topic] == 1 else 0
        stored = v.get("$SYS/broker/store/messages/count")
        if stored is not None:
            out["mqtt_backlog"] = max(0, stored - v.get("$SYS/broker/retained messages/count", 0))
        return out


def read_once(prev_cpu=None):
    now_cpu = cpu_times()
    payload = {"sensor": "sysmon", "timestamp": int(time.time())}
    values = {"cpu_temp": cpu_temp(), "cpu_load": cpu_load(prev_cpu, now_cpu), "mem_pct": mem_pct(),
              "disk_pct": disk_pct(), "fan_rpm": fan_rpm()}
    power, supply = parse_pmic(vcgencmd("pmic_read_adc"))
    values.update(power_w=power, supply_v=round(supply, 2) if supply else None)
    values.update(parse_throttled(vcgencmd("get_throttled")))
    values.update(service_health())
    payload.update({k: v for k, v in values.items() if v is not None})
    return payload, now_cpu


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="stdout")
    parser.add_argument("--interval", type=float, default=float(os.getenv("SYSMON_INTERVAL_S", "10")))
    args = parser.parse_args()
    publish_fn = make_publisher(args.mode, MQTT_TOPIC)
    broker = None
    if os.getenv("IMM_LOCAL_BROKER", "").lower() == "true" and args.mode != "stdout":
        broker = BrokerStatus(os.getenv("IMM_NODE_ID") or os.uname().nodename)
    _, prev = read_once()
    time.sleep(1)
    while True:
        payload, prev = read_once(prev)
        if broker is not None:
            payload.update(broker.read())
        if len(payload) > 2:
            publish_fn(payload)
        else:
            print(json.dumps({"error": "no node health readings available"}), file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
