#!/usr/bin/env python3
"""
The node's own MQTT broker: store-and-forward to the MCC.

Every IMM-OS service on the node publishes to (and subscribes on) the local broker at
localhost:1883. A Mosquitto bridge forwards the node's topics to the MCC broker over TLS
and brings the MCC's control topics back. While the MCC or the network is down the bridge
queues the readings on disk (persistent session, QoS 1) and delivers them, in order, when
the link returns; the node's own control loops (eclss_pid reads its sensors locally) keep
working meanwhile.

  habitat/sensors/#, habitat/eva/#, habitat/health/#, imm/habitat/#   node → MCC
  habitat/control/#                                                   MCC → node
  imm/bridge/<node>/state   (local only) "1" link up, "0" down: sysmon reports it

Config is rendered from /etc/imm-os/edge.env (MCC_HOST, MCC_MQTT_PORT, MCC_TLS_CA,
MCC_MQTT_USERNAME, MCC_MQTT_PASSWORD, IMM_NODE_ID) into
/etc/mosquitto/conf.d/imm-edge.conf (mode 640: it holds the MCC password).

  sudo .venv/bin/python tools/local_broker.py write     # (re)write; exit 3 = changed, restart mosquitto
  .venv/bin/python tools/local_broker.py status         # link state and queued messages
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envfile  # noqa: E402

ENV_FILE = os.getenv("IMM_ENV_FILE", "/etc/imm-os/edge.env")
CONF_PATH = os.getenv("IMM_BROKER_CONF", "/etc/mosquitto/conf.d/imm-edge.conf")
OUT_TOPICS = ("habitat/sensors/#", "habitat/eva/#", "habitat/health/#", "imm/habitat/#")
IN_TOPICS = ("habitat/control/#",)
MAX_QUEUED = 1_000_000          # ~a week of one node's readings; disk, not RAM


def state_topic(node: str) -> str:
    return f"imm/bridge/{node}/state"


def load(env_file: str = ENV_FILE) -> dict:
    lines = open(env_file).read().splitlines() if os.path.exists(env_file) else []
    get = lambda k, d="": envfile.get(lines, k) or d      # noqa: E731
    return {"node": get("IMM_NODE_ID", os.uname().nodename),
            "host": get("MCC_HOST") or get("MQTT_HOST", "imm.local"),
            "port": int(get("MCC_MQTT_PORT") or 8883),
            "ca": get("MCC_TLS_CA") or get("MQTT_TLS_CA"),
            "username": get("MCC_MQTT_USERNAME", "imm-edge"),
            "password": get("MCC_MQTT_PASSWORD") or get("MQTT_PASSWORD"),
            "listen": get("IMM_LOCAL_BROKER_PORT", "1883")}


def render(cfg: dict, standalone: bool = False, persistence_dir: str = None, restart: str = "2 30") -> str:
    """mosquitto.conf text. standalone=True adds what Debian's main mosquitto.conf already sets (tests)."""
    for k in ("node", "host", "port"):
        if not cfg.get(k):
            raise ValueError(f"missing {k}")
    for v in (cfg.get("password") or "", cfg.get("username") or ""):
        if any(c.isspace() for c in v):      # mosquitto.conf values end at the first space
            raise ValueError("MQTT credentials must not contain spaces or line breaks")
    node = cfg["node"]
    lines = ["# IMM-OS edge node: local broker + store-and-forward bridge to the MCC.",
             "# Written by tools/local_broker.py from /etc/imm-os/edge.env: edit that, not this file.", ""]
    if standalone:
        lines += ["persistence true", f"persistence_location {persistence_dir or '/var/lib/mosquitto/'}", ""]
    lines += [
        "# services on this node only",
        f"listener {cfg.get('listen', 1883)} 127.0.0.1",
        "allow_anonymous true",
        "",
        "# queue on disk while the MCC is unreachable",
        f"max_queued_messages {MAX_QUEUED}",
        "max_queued_bytes 0",
        "autosave_interval 30",
        "",
        "connection imm-mcc",
        f"address {cfg['host']}:{cfg['port']}",
    ]
    if cfg.get("ca"):
        lines += [f"bridge_cafile {cfg['ca']}", "bridge_insecure false"]
    if cfg.get("username"):
        lines.append(f"remote_username {cfg['username']}")
    if cfg.get("password"):
        lines.append(f"remote_password {cfg['password']}")
    lines += [
        f"remote_clientid imm-bridge-{node}",
        f"local_clientid imm-bridge-local-{node}",
        "bridge_protocol_version mqttv311",
        "# persistent session both ways: nothing is dropped while the link is down",
        "cleansession false",
        "keepalive_interval 30",
        f"restart_timeout {restart}",
        "start_type automatic",
        "try_private true",
        "bridge_attempt_unsubscribe false",
        "notifications true",
        "notifications_local_only true",
        f"notification_topic {state_topic(node)}",
    ]
    lines += [f"topic {t} out 1" for t in OUT_TOPICS]
    lines += [f"topic {t} in 1" for t in IN_TOPICS]
    return "\n".join(lines) + "\n"


def write(path: str, text: str) -> bool:
    """Atomic write, 0640 (group mosquitto when it exists). True if the file changed."""
    try:
        if open(path).read() == text:
            return False
    except OSError:
        pass
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o640)
    try:
        import grp
        os.chown(tmp, 0, grp.getgrnam("mosquitto").gr_gid)
    except (KeyError, PermissionError, ImportError):
        pass
    os.replace(tmp, path)
    return True


def status(node: str, port: int = 1883, timeout: float = 3.0) -> dict:
    """Bridge link state and the local broker's stored-message count."""
    import paho.mqtt.client as mqtt
    got = {}
    c = mqtt.Client()
    c.on_message = lambda cl, u, m: got.__setitem__(m.topic, m.payload.decode(errors="replace"))
    c.connect("127.0.0.1", port, 10)
    c.subscribe([(state_topic(node), 0), ("$SYS/broker/store/messages/count", 0)])
    c.loop_start()
    end = time.time() + timeout
    while time.time() < end and len(got) < 2:
        time.sleep(0.1)
    c.loop_stop()
    c.disconnect()
    link = got.get(state_topic(node))
    stored = got.get("$SYS/broker/store/messages/count")
    return {"link": None if link is None else link.strip() == "1",
            "stored": int(stored) if stored and stored.strip().isdigit() else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("write", "status", "show"))
    ap.add_argument("--conf", default=CONF_PATH)
    ap.add_argument("--env-file", default=ENV_FILE)
    args = ap.parse_args()
    cfg = load(args.env_file)
    if args.action == "show":
        print(render({**cfg, "password": "********" if cfg.get("password") else ""}), end="")
        return 0
    if args.action == "write":
        if not cfg.get("password"):
            print(f"MCC_MQTT_PASSWORD not set in {args.env_file}")
            return 2
        changed = write(args.conf, render(cfg))
        print(f"{args.conf} {'updated' if changed else 'unchanged'}: bridge to {cfg['host']}:{cfg['port']}")
        return 3 if changed else 0
    st = status(cfg["node"], int(cfg.get("listen") or 1883))
    link = {True: "UP", False: "DOWN (queuing)", None: "unknown (bridge not configured?)"}[st["link"]]
    print(f"MCC link {link}; {st['stored'] if st['stored'] is not None else '?'} message(s) stored in the local broker")
    return 0 if st["link"] else 1


if __name__ == "__main__":
    sys.exit(main())
