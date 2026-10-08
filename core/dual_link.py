"""
Two links to one sensor board (USB serial and Wi-Fi), one stream of readings.

A board can send every reading over its USB cable to the Pi and also serve it over Wi-Fi. With
both links read at once, losing either one loses nothing: the other carries on, and every reading
still reaches the SD card, the blackbox and (now or once the laptop is reachable) the dashboard.

Both links feed one queue; the reader's main loop takes each reading from it and publishes it
once, whichever link brought it first:

  - a reading carrying the board's own counter (the IMM-OS firmware's "ms", or a sketch's
    uptime) is published once; the copy that arrives over the other link is dropped;
  - a reading without one is taken from USB while USB is delivering, and from Wi-Fi only after
    USB has been quiet for HOLD_S (so nothing is doubled, and a USB fault costs a few seconds).

Each published reading says which link brought it ("via": "usb" or "wifi"), and the board's health
reading carries usb_link / wifi_link: 1 when that link delivered in the last LINK_S seconds.

The links run in helper threads; publishing stays in the main thread, which also keeps the
systemd watchdog fed (it only counts kicks from the main thread).
"""
import collections
import queue
import threading
import time

HOLD_S = 3.0            # USB quiet this long: take readings without a counter from Wi-Fi
LINK_S = 15.0           # a link is "up" if it delivered within this long


class DualLink:
    def __init__(self, clock=time.monotonic, window: int = 600):
        self.clock = clock
        self.seen = collections.deque(maxlen=window)        # recent board counters, in arrival order
        self.seen_set = set()
        self.last = {"usb": None, "wifi": None}
        self.queue = queue.Queue(maxsize=2000)
        self.stop = threading.Event()

    def put(self, via: str, line) -> None:
        """From a link's thread: a reading arrived (never blocks; a full queue drops the oldest)."""
        try:
            self.queue.put_nowait((via, line))
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait((via, line))

    def accept(self, via: str, key=None) -> bool:
        """Should this reading be published? key: the board's counter for it, if it has one."""
        now = self.clock()
        self.last[via] = now
        if key is not None:
            if key in self.seen_set:
                return False                                  # the other link brought it first
            if self.seen and key < min(self.seen):            # the board restarted: its counter began again
                self.seen.clear()
                self.seen_set.clear()
            if len(self.seen) == self.seen.maxlen:
                self.seen_set.discard(self.seen[0])
            self.seen.append(key)
            self.seen_set.add(key)
            return True
        usb = self.last["usb"]
        return via == "usb" or usb is None or now - usb >= HOLD_S

    def links(self) -> dict:
        now = self.clock()
        return {f"{via}_link": int(t is not None and now - t < LINK_S) for via, t in self.last.items()}

    def run(self, target, *args) -> threading.Thread:
        """Start one link's reader: target(self, *args) puts readings until self.stop is set."""
        t = threading.Thread(target=target, args=(self, *args), daemon=True)
        t.start()
        return t


class LinkReport:
    """Say in the journal when a link stops or starts delivering (not every reading)."""

    def __init__(self, board: str, out):
        self.board, self.out, self.state = board, out, {}

    def update(self, links: dict) -> None:
        for name, up in links.items():
            was = self.state.get(name)
            if was is not None and was != up:
                what = "USB cable" if name == "usb_link" else "Wi-Fi"
                self.out({"info" if up else "error":
                          f"{self.board}: {what} link {'delivering' if up else 'silent'}"
                          + ("" if up else ", readings carried by the other link")})
            self.state[name] = up
