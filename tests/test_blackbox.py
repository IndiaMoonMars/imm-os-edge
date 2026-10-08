"""core/blackbox_logger.py: the offline record of everything a node publishes."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytest.importorskip("google.protobuf")
sys.path.insert(0, os.path.join(ROOT, "core"))
import telemetry_pb2  # noqa: E402


def chunks(path):
    with open(path, "rb") as f:
        while head := f.read(4):
            c = telemetry_pb2.TelemetryChunk()
            c.ParseFromString(f.read(int.from_bytes(head, "big")))
            yield c


def test_fractional_timestamps_are_recorded(tmp_path):
    """Drivers send ms timestamps (1790000000.123): every one of them used to fail with
    "'float' object cannot be interpreted as an integer" and nothing was recorded."""
    lines = [{"data": {"sensor": "bme280", "timestamp": 1790000000.123 + i, "temp": 24.0}, "sig": "s"} for i in range(3)]
    lines.append({"sensor": "ecg_ad8232", "timestamp": 1790000003.51, "voltage": 1.6})   # no envelope
    r = subprocess.run([sys.executable, os.path.join(ROOT, "core", "blackbox_logger.py")],
                       input="\n".join(json.dumps(x) for x in lines) + "\n", capture_output=True, text=True,
                       env={**os.environ, "IMM_BLACKBOX_DIR": str(tmp_path)}, timeout=30)
    assert r.returncode == 0 and "error" not in r.stderr, r.stderr
    [pb] = list(tmp_path.glob("telemetry_*.pb"))
    recs = [rec for c in chunks(pb) for rec in c.records]
    assert [(x.sensor_type, x.timestamp) for x in recs] == [("bme280", 1790000000), ("bme280", 1790000001),
                                                            ("bme280", 1790000002), ("ecg_ad8232", 1790000003)]
    assert json.loads(recs[0].payload_json)["timestamp"] == 1790000000.123     # full precision kept
    assert recs[0].signature == "s" and recs[3].signature == "unsigned"


def test_replay_republishes_the_window_as_delayed_readings(tmp_path):
    """driver → encryption layer → blackbox, then replay through a real broker."""
    import shutil
    import socket
    import time
    mosq = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
    if not os.path.exists(mosq):
        pytest.skip("mosquitto not installed")
    mqtt = pytest.importorskip("paho.mqtt.client")
    base = 1790000000.0
    lines = [{"sensor": "scd40", "zone": "zone_a", "node_id": "n1", "co2_ppm": 600 + i, "timestamp": base + i,
              "seq": i + 1, "run": "r1"} for i in range(10)]
    lines.append({"sensor": "eva_biosensor", "crew_id": "ev1", "node_id": "n1", "hr_bpm": 90, "timestamp": base + 5})
    env = {**os.environ, "IMM_BLACKBOX_DIR": str(tmp_path), "IMM_SECRET_KEY": "k"}
    enc = subprocess.run([sys.executable, os.path.join(ROOT, "core", "encryption_layer.py")],
                         input="\n".join(json.dumps(x) for x in lines) + "\n", capture_output=True, text=True, env=env, timeout=30)
    subprocess.run([sys.executable, os.path.join(ROOT, "core", "blackbox_logger.py")], input=enc.stdout,
                   capture_output=True, text=True, env=env, timeout=30, check=True)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    conf = tmp_path / "m.conf"
    conf.write_text(("user root\n" if os.geteuid() == 0 else "") + f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    broker = subprocess.Popen([mosq, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        got = []
        sub = mqtt.Client()
        sub.on_message = lambda c, u, m: got.append((m.topic, json.loads(m.payload)))
        sub.connect("127.0.0.1", port, 30)
        sub.subscribe("habitat/#", 1)
        sub.loop_start()
        time.sleep(0.3)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "core", "blackbox_replay.py"), "--since", str(base + 3),
                            "--until", str(base + 7), "--dir", str(tmp_path), "--rate", "1000"],
                           env={**env, "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(port)}, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, r.stderr
        time.sleep(0.5)
        sub.loop_stop()
    finally:
        broker.terminate()
    co2 = [(t, p["seq"], p["delayed"]) for t, p in got if t.startswith("habitat/sensors")]
    assert co2 == [("habitat/sensors/scd40/zone_a", i, True) for i in range(4, 9)]      # t = base+3 .. base+7
    assert [t for t, _ in got if "eva" in t] == ["habitat/eva/biosensors/ev1"]
    assert all(p["run"] == "r1" for t, p in got if t.startswith("habitat/sensors"))  # same run: fills the MCC's gap
