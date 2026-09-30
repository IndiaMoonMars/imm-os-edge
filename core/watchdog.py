"""
systemd watchdog for the IMM-OS edge daemons (stdlib only, any systemd version).

The units set WatchdogSec= and NotifyAccess=all. Each daemon calls kick() from its main
loop: when the loop stops going round (I²C lock-up, a read that never returns, a full
pipe to the blackbox) the kicks stop, systemd kills the service (SIGABRT: with
PYTHONFAULTHANDLER=1 the journal gets the stack where it was stuck) and restarts it.

kick() only counts from the main thread: a helper thread that is still alive must not
hide a hung main loop. Outside systemd (NOTIFY_SOCKET unset) everything here is a no-op.

  kick()          the main loop is alive (rate-limited, call it as often as you like)
  sleep(s)        kick, then sleep s seconds (kicking every few seconds of a long sleep)
  status(text)    shown by `systemctl status`
"""
import os
import socket
import threading
import time

MIN_INTERVAL_S = 1.0
_last = 0.0
_sock = None


def _send(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):                 # abstract namespace
        addr = "\0" + addr[1:]
    global _sock
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _sock.sendto(message.encode(), addr)
        return True
    except OSError:
        return False


def enabled() -> bool:
    return bool(os.environ.get("NOTIFY_SOCKET") and os.environ.get("WATCHDOG_USEC"))


def kick() -> None:
    global _last
    if threading.current_thread() is not threading.main_thread():
        return
    t = time.monotonic()
    if t - _last < MIN_INTERVAL_S:
        return
    _last = t
    _send("WATCHDOG=1")


def sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while True:
        kick()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 5.0))


def status(text: str) -> None:
    _send(f"STATUS={text}")
