"""
Shared hardware helpers for IMM-OS edge scripts.

Every ECLSS/EVA script talks to real hardware by default. Pass --simulate (or set
IMM_SIMULATE=true) to run the old synthetic behaviour on a laptop or in CI.

GPIO uses gpiozero, which works on Raspberry Pi 4 and 5 (lgpio backend); I2C uses
smbus2; serial ports use pyserial. Imports are lazy so --simulate needs none of them.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("imm.hw")


def simulate_requested(flag: bool = False) -> bool:
    return flag or os.getenv("IMM_SIMULATE", "false").lower() == "true"


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)), 0)  # accepts 0x.. for I2C addresses


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# ── GPIO ──────────────────────────────────────────────────────────

def digital_input(pin: int, pull_up: bool = True):
    from gpiozero import DigitalInputDevice
    return DigitalInputDevice(pin, pull_up=pull_up)


class Relay:
    """
    One relay channel. De-energised = OFF = the safe state, so a crash, reboot or
    lost sensor data always leaves the load switched off.
    Most 5 V relay boards are active-low (RELAY_ACTIVE_LOW=true, the default).
    """

    def __init__(self, name: str, pin: int, active_low: bool = None):
        from gpiozero import OutputDevice
        if active_low is None:
            active_low = os.getenv("RELAY_ACTIVE_LOW", "true").lower() == "true"
        self.name = name
        self._dev = OutputDevice(pin, active_high=not active_low, initial_value=False)

    @property
    def is_on(self) -> bool:
        return bool(self._dev.value)

    def set(self, on: bool) -> None:
        if on != self.is_on:
            self._dev.on() if on else self._dev.off()
            log.info("%s relay %s", self.name, "ON" if on else "OFF")

    def close(self) -> None:
        self._dev.off()
        self._dev.close()


class LogRelay:
    """Stand-in relay for --simulate: logs switching only."""

    def __init__(self, name: str):
        self.name, self.is_on = name, False

    def set(self, on: bool) -> None:
        if on != self.is_on:
            self.is_on = on
            log.info("%s relay %s (simulated)", self.name, "ON" if on else "OFF")

    def close(self) -> None:
        self.is_on = False


# ── Posting events to IMM-OS APIs, surviving network outages ──────

class EventPoster:
    """
    POST events to an IMM-OS API as the edge device. Events that can't be delivered
    (API down, network outage) are appended to a spool file and re-sent, oldest
    first, before the next event, so nothing measured during an outage is lost.
    Events the API rejects (4xx) are logged and dropped: re-sending won't fix them.
    """

    def __init__(self, url: str, name: str, spool_dir: str = None):
        self.url = url
        spool_dir = spool_dir or os.getenv("IMM_SPOOL_DIR", "/var/lib/imm-os/spool")
        self.spool = Path(spool_dir) / f"{name}.jsonl"
        self._lock = threading.Lock()

    def _send(self, payload: dict) -> str:
        import requests
        from auth_client import auth_headers
        try:
            resp = requests.post(self.url, json=payload, headers=auth_headers(), timeout=5)
        except Exception as exc:  # connection refused, DNS, timeout
            log.warning("%s unreachable (%s); spooling event", self.url, exc.__class__.__name__)
            return "retry"
        if resp.status_code >= 500 or resp.status_code in (401, 403):
            # server trouble, or the token was refused (clock skew, Keycloak restart): keep it
            log.warning("%s → HTTP %s; spooling event", self.url, resp.status_code)
            return "retry"
        if resp.status_code >= 400:
            log.error("%s rejected event (HTTP %s): %s", self.url, resp.status_code, resp.text[:160])
            return "drop"
        return "ok"

    def _append(self, payload: dict) -> None:
        try:
            self.spool.parent.mkdir(parents=True, exist_ok=True)
            with self.spool.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        except OSError as exc:
            log.error("Cannot spool event to %s: %s — event lost", self.spool, exc)

    def flush(self) -> bool:
        """Re-send spooled events; returns True when the spool is empty."""
        if not self.spool.exists():
            return True
        lines = [ln for ln in self.spool.read_text().splitlines() if ln.strip()]
        remaining = []
        for i, line in enumerate(lines):
            if self._send(json.loads(line)) == "retry":
                remaining = lines[i:]
                break
        if remaining:
            self.spool.write_text("\n".join(remaining) + "\n")
            return False
        self.spool.unlink(missing_ok=True)
        if lines:
            log.info("Delivered %d spooled event(s)", len(lines))
        return True

    def post(self, payload: dict) -> str:
        """Returns "ok" (delivered), "spooled" (will be re-sent) or "rejected" (dropped)."""
        with self._lock:
            if not self.flush():
                self._append(payload)
                return "spooled"
            result = self._send(payload)
            if result == "retry":
                self._append(payload)
                return "spooled"
            return "ok" if result == "ok" else "rejected"


def sleep_until(deadline: float) -> None:
    time.sleep(max(0.0, deadline - time.monotonic()))
