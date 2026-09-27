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
