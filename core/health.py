"""
Component status for the MCC's health monitor (FDIR): habitat/health/<node>/<component>.

    health = ComponentHealth("eclss_pid", interval_s=30)
    health.set("SAFE", "no fresh temperature for 120 s: HVAC and dehumidifier off")
    health.tick()          # from the main loop: re-sends the state every interval_s

The state is published at once when it changes and repeated every interval_s, so the
MCC notices both a change and silence (a component that stops reporting). Retained,
so after a restart of the MCC (or of the link) the current state is known at once.
Through the local broker it is queued like any reading while the MCC is away.

States (what the component can still do):
  NOMINAL    working normally
  DEGRADED   working with less (a backup sensor, one of two loops disabled)
  SAFE       outputs held in their safe state (relays off) until inputs return
  ISOLATED   a faulty part switched out, the rest working
  FAULT      not working
  STARTING / STOPPED
"""
import json
import logging
import os
import socket
import time

STATES = ("NOMINAL", "DEGRADED", "SAFE", "ISOLATED", "FAULT", "STARTING", "STOPPED")
log = logging.getLogger("health")


class ComponentHealth:
    def __init__(self, component: str, interval_s: float = 30.0, client=None, node: str = None):
        self.component = component
        self.node = node or os.getenv("IMM_NODE_ID") or socket.gethostname()
        self.topic = f"habitat/health/{self.node}/{component}"
        self.interval_s = interval_s
        self.state, self.reason, self.details = "STARTING", "", {}
        self._sent_at = 0.0
        self._client = client

    def _client_or_new(self):
        if self._client is None:
            from mqtt_publisher import create_client
            self._client = create_client()
        return self._client

    def set(self, state: str, reason: str = "", **details) -> bool:
        """Report a state; published now if it (or its reason) changed. True if it changed."""
        if state not in STATES:
            raise ValueError(state)
        changed = (state, reason) != (self.state, self.reason)
        self.state, self.reason, self.details = state, reason, details
        if changed:
            (log.warning if state in ("SAFE", "FAULT", "DEGRADED", "ISOLATED") else log.info)(
                "%s → %s%s", self.component, state, f": {reason}" if reason else "")
            self.publish()
        return changed

    def tick(self) -> None:
        if time.monotonic() - self._sent_at >= self.interval_s:
            self.publish()

    def publish(self) -> None:
        self._sent_at = time.monotonic()
        msg = {"node_id": self.node, "component": self.component, "state": self.state, "reason": self.reason,
               "timestamp": round(time.time(), 3), "interval_s": self.interval_s, "details": self.details}
        try:
            self._client_or_new().publish(self.topic, json.dumps(msg), qos=1, retain=True)
        except Exception as exc:      # reporting must never take the component down
            log.debug("health publish failed: %s", exc)
