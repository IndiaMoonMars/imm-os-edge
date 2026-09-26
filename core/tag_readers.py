"""
Tag readers shared by the inventory barcode listener, the waste tracker and the EVA
tool station.

  USB HID readers (barcode scanners, most USB RFID readers) "type" the code and
  press Enter. read_hid() reads one directly via Linux evdev and grabs it, so
  scans don't land in whichever window has focus.

  RC522 (MFRC522, SPI) RFID modules: read_rc522() polls for cards and yields the
  card UID in hex, once per presentation.

open_tag_reader() picks one from the environment:
  <PREFIX>_DEVICE=/dev/input/by-id/usb-...-event-kbd   → USB HID reader
  <PREFIX>_BACKEND=rc522                               → RC522 on SPI0
  <PREFIX>_BACKEND=stdin                               → type tags (testing)
"""
import logging
import os
import sys
import time
from typing import Iterator

log = logging.getLogger("imm.tags")

_CHARS = {**{f"KEY_{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
          **{f"KEY_{d}": d for d in "0123456789"},
          "KEY_MINUS": "-", "KEY_DOT": ".", "KEY_SLASH": "/", "KEY_SPACE": " ",
          "KEY_EQUAL": "=", "KEY_COMMA": ",", "KEY_SEMICOLON": ";"}
_SHIFTED = {"KEY_MINUS": "_", "KEY_SLASH": "?", "KEY_EQUAL": "+", "KEY_SEMICOLON": ":"}


def decode_keys(events) -> Iterator[str]:
    """
    Turn (keycode, state) pairs into lines. state: 1 = down, 0 = up, 2 = repeat.
    Pure function so it can be tested without a device.
    """
    buf, shift = [], False
    for name, state in events:
        if name in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
            shift = state != 0
            continue
        if state != 1:
            continue
        if name in ("KEY_ENTER", "KEY_KPENTER"):
            if buf:
                yield "".join(buf)
            buf = []
        elif name in _CHARS:
            c = _SHIFTED.get(name) if shift and name in _SHIFTED else _CHARS[name]
            buf.append(c.upper() if shift and c.isalpha() else c)


def read_hid(path: str) -> Iterator[str]:
    from evdev import InputDevice, categorize, ecodes  # Linux only (pip install evdev)

    dev = InputDevice(path)
    dev.grab()  # scans go only to us, not to the focused window
    log.info("Reading %s (%s)", dev.name, path)

    def events():
        for event in dev.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            key = categorize(event)
            name = key.keycode if isinstance(key.keycode, str) else key.keycode[0]
            yield name, key.keystate

    try:
        yield from decode_keys(events())
    finally:
        dev.ungrab()


def read_rc522(poll_s: float = 0.2, rearm_s: float = 2.0) -> Iterator[str]:
    """Yield each card's UID (hex) when presented; the same card again only after it was removed for rearm_s."""
    from rc522 import KNOWN_VERSIONS, RC522  # core/rc522.py (spidev; works on Pi 4 and 5)

    reader = RC522(bus=int(os.getenv("RC522_SPI_BUS", "0")), device=int(os.getenv("RC522_SPI_CS", "0")))
    ver = reader.version()
    if ver in (0x00, 0xFF):
        raise SystemExit(f"RC522 not answering on SPI (version register 0x{ver:02X}): check wiring and that SPI is enabled")
    log.info("Reading RC522 RFID on SPI0 (%s)", KNOWN_VERSIONS.get(ver, f"version 0x{ver:02X}"))
    last_uid, last_seen = None, 0.0
    while True:
        uid = reader.read_uid()
        now = time.monotonic()
        if uid:
            if uid != last_uid or now - last_seen > rearm_s:
                yield uid
            last_uid, last_seen = uid, now
        time.sleep(poll_s)


def read_stdin() -> Iterator[str]:
    for line in sys.stdin:
        if line.strip():
            yield line.strip()


def open_tag_reader(prefix: str) -> Iterator[str]:
    device = os.getenv(f"{prefix}_DEVICE")
    backend = os.getenv(f"{prefix}_BACKEND", "hid" if device else "rc522").lower()
    if backend == "stdin":
        return read_stdin()
    if backend == "rc522":
        return read_rc522()
    if not device:
        raise SystemExit(f"Set {prefix}_DEVICE to the reader's /dev/input/by-id/... path "
                         f"or {prefix}_BACKEND=rc522")
    return read_hid(device)
