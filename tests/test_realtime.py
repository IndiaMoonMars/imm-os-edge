"""Realtime behaviour of the edge drivers (no hardware needed)."""
import pytest

import ecg_driver
import lux_driver
import mqtt_publisher


class FakeClient:
    def __init__(self):
        self.published, self.queued, self.connected_async = [], None, False

    def username_pw_set(self, *a):
        pass

    def tls_set(self, **kw):
        pass

    def max_queued_messages_set(self, n):
        self.queued = n

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, host, port, keepalive):
        self.connected_async = True

    def loop_start(self):
        pass

    def publish(self, topic, msg, qos):
        self.published.append((topic, msg, qos))

        class R:  # a real MQTTMessageInfo; waiting on it would block the sampling loop
            def wait_for_publish(self, timeout=None):
                raise AssertionError("publish must not wait for the broker")
        return R()


def test_publisher_is_non_blocking_and_bounded(monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(mqtt_publisher.mqtt, "Client", lambda **kw: fake)
    monkeypatch.setenv("IMM_CALIBRATION_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("IMM_NODE_ID", "node-rpi-01")
    publish = mqtt_publisher.make_publisher("mqtt", "habitat/sensors/ecg_ad8232/zone1")
    for i in range(100):
        publish({"sensor": "ecg_ad8232", "voltage": 1.6, "timestamp": 1.0 + i / 100})
    assert len(fake.published) == 100 and fake.published[0][2] == 1
    assert fake.connected_async and fake.queued == mqtt_publisher.MAX_QUEUED   # starts even if MCC is down


def test_ecg_schedule_keeps_rate_and_skips_after_a_stall():
    period = 0.01
    n, due = ecg_driver.schedule(100.0, period, 100.0, 0)
    assert (n, due) == (0, 100.0)
    n, due = ecg_driver.schedule(100.0, period, 100.004, 1)          # on time: next slot
    assert n == 1 and due == pytest.approx(100.01)
    n, due = ecg_driver.schedule(100.0, period, 101.0, 2)            # stalled 1 s: jump ahead, no burst
    assert due > 101.0 and due - 101.0 <= period + 1e-9


def test_lux_zones_are_lower_case_and_validated():
    assert lux_driver.parse_zones("Zone_A:0, zone_b:1") == {"zone_a": 0, "zone_b": 1}
    with pytest.raises(ValueError):
        lux_driver.parse_zones("zone_a:9")
