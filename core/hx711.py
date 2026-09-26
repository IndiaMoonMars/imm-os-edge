"""
HX711 24-bit load-cell ADC, bit-banged with lgpio (Raspberry Pi 4 and 5).

The HX711 powers down if its clock stays high for more than 60 µs, so it is driven
with lgpio directly rather than a higher-level GPIO library. Readings that look
corrupted (clock stretched by the scheduler) are discarded by taking the median.

Calibration (weight = (raw − offset) / scale):
  python3 eclss/waste_tracker.py --calibrate 1.0     # with a known 1.0 kg on the scale
prints HX711_SCALE for /etc/imm-os/edge.env; the offset (tare) is measured at start-up
with the empty bin.
"""
import statistics
import time


class HX711:
    def __init__(self, dout: int, sck: int, chip: int = None, gain_pulses: int = 1):
        import lgpio
        self._lg = lgpio
        # Pi 5 exposes the header GPIOs on gpiochip4 on older kernels, gpiochip0 on newer
        chips = [chip] if chip is not None else [0, 4]
        for c in chips:
            try:
                self.h = lgpio.gpiochip_open(c)
                lgpio.gpio_claim_input(self.h, dout)
                lgpio.gpio_claim_output(self.h, sck, 0)
                break
            except lgpio.error:
                self.h = None
        if self.h is None:
            raise RuntimeError(f"Cannot claim HX711 pins DOUT={dout} SCK={sck}")
        self.dout, self.sck = dout, sck
        self.gain_pulses = gain_pulses  # 1 → channel A gain 128
        self.offset, self.scale = 0.0, 1.0

    def _ready(self) -> bool:
        return self._lg.gpio_read(self.h, self.dout) == 0

    def read_raw(self, timeout: float = 1.0) -> int:
        deadline = time.monotonic() + timeout
        while not self._ready():
            if time.monotonic() > deadline:
                raise TimeoutError("HX711 not ready (check wiring/power)")
            time.sleep(0.001)
        value = 0
        w, r = self._lg.gpio_write, self._lg.gpio_read
        for _ in range(24):
            w(self.h, self.sck, 1)
            w(self.h, self.sck, 0)
            value = (value << 1) | r(self.h, self.dout)
        for _ in range(self.gain_pulses):
            w(self.h, self.sck, 1)
            w(self.h, self.sck, 0)
        if value & 0x800000:          # two's complement
            value -= 1 << 24
        return value

    def read_median(self, n: int = 9) -> float:
        samples = []
        for _ in range(n * 2):
            try:
                samples.append(self.read_raw())
            except TimeoutError:
                continue
            if len(samples) == n:
                break
        if not samples:
            raise TimeoutError("HX711 returned no samples")
        return statistics.median(samples)

    def tare(self, n: int = 15) -> None:
        self.offset = self.read_median(n)

    def weight(self, n: int = 9) -> float:
        return (self.read_median(n) - self.offset) / self.scale

    def close(self) -> None:
        self._lg.gpiochip_close(self.h)
