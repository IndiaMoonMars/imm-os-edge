"""
Store-and-forward with real brokers: an edge broker (tools/local_broker.py config) bridged
to an "MCC" broker. Readings published while the MCC is down reach it, in order and
exactly once, when it comes back, even across a restart of the edge broker.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import local_broker  # noqa: E402

MOSQ = shutil.which("mosquitto") or ("/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)
mqtt = pytest.importorskip("paho.mqtt.client")
needs_broker = pytest.mark.skipif(MOSQ is None, reason="mosquitto not installed")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Broker:
    def __init__(self, path, text):
        self.conf = str(path / "mosquitto.conf")
        extra = "user root\n" if os.geteuid() == 0 else ""
        open(self.conf, "w").write(extra + text)
        self.port = int(next(l.split()[1] for l in text.splitlines() if l.startswith("listener ")))
        self.p = None

    def start(self):
        self.p = subprocess.Popen([MOSQ, "-c", self.conf], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return self
            except OSError:
                if self.p.poll() is not None:
                    raise RuntimeError(self.p.stderr.read().decode())
                time.sleep(0.05)
        raise RuntimeError("broker did not start")

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()                 # graceful: saves the persistence file
            self.p.wait(10)


def client(port, cid="", clean=True):
    c = mqtt.Client(client_id=cid, clean_session=clean)
    c.connect("127.0.0.1", port, 30)
    c.loop_start()
    return c


def publish_all(port, n, start=1):
    c = client(port)
    infos = [c.publish("habitat/sensors/bme280/zone_a", json.dumps({"seq": i, "temp": 22.0}), qos=1)
             for i in range(start, start + n)]
    for i in infos:
        i.wait_for_publish()
    c.loop_stop()
    c.disconnect()


class Collector:
    """A persistent-session subscriber on the MCC broker (like the MQTT→Kafka bridge)."""

    def __init__(self, port):
        self.port, self.got, self.cid = port, [], "mcc-bridge-test"

    def register(self):
        c = client(self.port, self.cid, clean=False)
        c.subscribe("habitat/#", qos=1)
        time.sleep(0.3)
        c.loop_stop()
        c.disconnect()

    def collect(self, want, timeout=20):
        c = mqtt.Client(client_id=self.cid, clean_session=False)
        c.on_message = lambda cl, u, m: self.got.append(json.loads(m.payload)["seq"])
        c.connect("127.0.0.1", self.port, 30)
        c.loop_start()
        end = time.time() + timeout
        while time.time() < end and len(self.got) < want:
            time.sleep(0.1)
        time.sleep(0.5)                       # anything extra (duplicates) arrives too
        c.loop_stop()
        c.disconnect()
        return self.got


@pytest.fixture
def pair(tmp_path):
    (tmp_path / "mcc").mkdir()
    (tmp_path / "edge").mkdir()
    mcc_port, edge_port = free_port(), free_port()
    mcc = Broker(tmp_path / "mcc", f"listener {mcc_port} 127.0.0.1\nallow_anonymous true\n"
                                   f"persistence true\npersistence_location {tmp_path / 'mcc'}/\n")
    cfg = {"node": "node-test", "host": "127.0.0.1", "port": mcc_port, "ca": "", "username": "", "password": "",
           "listen": edge_port}
    edge = Broker(tmp_path / "edge", local_broker.render(cfg, standalone=True, persistence_dir=f"{tmp_path / 'edge'}/",
                                                         restart="1 2") + "sys_interval 1\n")
    yield mcc, edge
    edge.stop()
    mcc.stop()


def test_render_has_the_bridge_contract():
    text = local_broker.render({"node": "node-rpi-01", "host": "imm.local", "port": 8883, "ca": "/etc/imm-os/mqtt-ca.crt",
                                "username": "imm-edge", "password": "s3cret"})
    for line in ("listener 1883 127.0.0.1", "address imm.local:8883", "bridge_cafile /etc/imm-os/mqtt-ca.crt",
                 "cleansession false", "remote_clientid imm-bridge-node-rpi-01", "topic habitat/sensors/# out 1",
                 "topic habitat/health/# out 1", "topic habitat/control/# in 1",
                 "notification_topic imm/bridge/node-rpi-01/state", "notifications_local_only true"):
        assert line in text.splitlines(), line
    with pytest.raises(ValueError):
        local_broker.render({"node": "n", "host": "h", "port": 1, "password": "a\nlistener 0"})


def test_write_is_atomic_private_and_reports_change(tmp_path):
    p = str(tmp_path / "imm-edge.conf")
    assert local_broker.write(p, "a\n") is True
    assert oct(os.stat(p).st_mode & 0o777) == "0o640"
    assert local_broker.write(p, "a\n") is False


@needs_broker
def test_readings_published_during_an_mcc_outage_arrive_in_order(pair):
    mcc, edge = pair
    col = Collector(mcc.port)
    mcc.start()
    col.register()                            # the MCC-side consumer's session exists
    mcc.stop()                                # MCC down
    edge.start()
    publish_all(edge.port, 300)               # the node keeps publishing locally
    # what sysmon reports to the MCC meanwhile (it arrives, queued, once the link is back)
    sys.path.insert(0, os.path.join(ROOT, "sensor_drivers"))
    os.environ["IMM_LOCAL_BROKER_PORT"] = str(edge.port)
    import sysmon_driver
    st = sysmon_driver.BrokerStatus("node-test")
    end = time.time() + 10
    while time.time() < end and st.read().get("mqtt_backlog", 0) < 300:
        time.sleep(0.2)
    assert st.read() == {"mcc_link": 0, "mqtt_backlog": 300}
    mcc.start()                               # link back: the bridge flushes its queue
    got = col.collect(300)
    assert got == list(range(1, 301))
    end = time.time() + 10
    while time.time() < end and st.read() != {"mcc_link": 1, "mqtt_backlog": 0}:
        time.sleep(0.2)
    assert st.read() == {"mcc_link": 1, "mqtt_backlog": 0}
    st.client.loop_stop()


@needs_broker
def test_queue_survives_a_restart_of_the_node_broker(pair):
    mcc, edge = pair
    col = Collector(mcc.port)
    mcc.start()
    col.register()
    mcc.stop()
    edge.start()
    publish_all(edge.port, 50)
    edge.stop()                               # e.g. the Pi rebooted during the outage
    edge.start()
    publish_all(edge.port, 50, start=51)
    mcc.start()
    assert col.collect(100) == list(range(1, 101))


@needs_broker
def test_control_commands_come_back_and_link_state_is_reported(pair):
    mcc, edge = pair
    mcc.start()
    edge.start()
    got = {}
    local = mqtt.Client()
    local.on_message = lambda c, u, m: got.__setitem__(m.topic, m.payload.decode())
    local.connect("127.0.0.1", edge.port, 30)
    local.subscribe([("habitat/control/#", 1), (local_broker.state_topic("node-test"), 0)])
    local.loop_start()
    end = time.time() + 15
    while time.time() < end and got.get(local_broker.state_topic("node-test")) != "1":
        time.sleep(0.1)
    assert got.get(local_broker.state_topic("node-test")) == "1"
    c = client(mcc.port)
    c.publish("habitat/control/lighting/core", json.dumps({"brightness": 50, "kelvin": 4000}), qos=1, retain=True).wait_for_publish()
    end = time.time() + 10
    while time.time() < end and "habitat/control/lighting/core" not in got:
        time.sleep(0.1)
    assert json.loads(got["habitat/control/lighting/core"])["kelvin"] == 4000
    mcc.stop()
    end = time.time() + 15
    while time.time() < end and got.get(local_broker.state_topic("node-test")) != "0":
        time.sleep(0.1)
    assert got[local_broker.state_topic("node-test")] == "0"           # sysmon reports mcc_link 0
    local.loop_stop()
