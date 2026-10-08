"""
Minimal MAX30100 pulse-oximeter driver (I2C, smbus2).

Replaces the unpublished `max30100` package the drivers used to import. Same
interface (MAX30100(), enable_spo2(), read_sensor() → .ir/.red) plus read_fifo(),
which returns every sample since the last call so a 100 Hz stream has no gaps or
duplicates regardless of how often the caller polls.

Configuration: SpO2 mode, 100 samples/s, 1600 µs pulses (16-bit), 27.1 mA LEDs.
The MAX30102 (part ID 0x15) uses a different register map and is rejected clearly.
"""
from typing import List, Tuple

ADDRESS = 0x57
REG_FIFO_WR_PTR = 0x02
REG_OVF_COUNTER = 0x03
REG_FIFO_RD_PTR = 0x04
REG_FIFO_DATA = 0x05
REG_MODE_CONFIG = 0x06
REG_SPO2_CONFIG = 0x07
REG_LED_CONFIG = 0x09
REG_PART_ID = 0xFF
PART_ID_MAX30100 = 0x11
PART_ID_MAX30102 = 0x15

MODE_RESET = 0x40
MODE_HR = 0x02
MODE_SPO2 = 0x03
SPO2_HI_RES = 0x40
SR_100HZ = 0x01 << 2
PW_1600US = 0x03
LED_27MA = 0x8
FIFO_DEPTH = 16


def decode_samples(data: bytes) -> List[Tuple[int, int]]:
    """FIFO bytes (4 per sample: IR hi, IR lo, RED hi, RED lo) → [(ir, red), …]."""
    return [((data[i] << 8) | data[i + 1], (data[i + 2] << 8) | data[i + 3]) for i in range(0, len(data) - 3, 4)]


class MAX30100:
    def __init__(self, bus: int = 1, address: int = ADDRESS, led_ir: int = LED_27MA, led_red: int = LED_27MA):
        from smbus2 import SMBus
        self.bus = SMBus(bus)
        self.addr = address
        part = self.bus.read_byte_data(self.addr, REG_PART_ID)
        if part == PART_ID_MAX30102:
            raise RuntimeError("found a MAX30102 (part ID 0x15): this driver supports the MAX30100 only")
        if part != PART_ID_MAX30100:
            raise RuntimeError(f"unexpected part ID 0x{part:02X} at 0x{address:02X} (MAX30100 is 0x11)")
        self.bus.write_byte_data(self.addr, REG_MODE_CONFIG, MODE_RESET)
        self._wait_reset()
        self.bus.write_byte_data(self.addr, REG_SPO2_CONFIG, SPO2_HI_RES | SR_100HZ | PW_1600US)
        self.bus.write_byte_data(self.addr, REG_LED_CONFIG, ((led_red & 0x0F) << 4) | (led_ir & 0x0F))
        self.bus.write_byte_data(self.addr, REG_MODE_CONFIG, MODE_HR)
        self._clear_fifo()
        self.ir = self.red = 0

    def _wait_reset(self, timeout_s: float = 0.1) -> None:
        """The RESET bit clears itself once the chip is ready; registers written before that are lost."""
        import time
        deadline = time.monotonic() + timeout_s
        while self.bus.read_byte_data(self.addr, REG_MODE_CONFIG) & MODE_RESET:
            if time.monotonic() > deadline:
                raise RuntimeError("MAX30100 did not come out of reset")
            time.sleep(0.002)

    def _clear_fifo(self) -> None:
        for reg in (REG_FIFO_WR_PTR, REG_OVF_COUNTER, REG_FIFO_RD_PTR):
            self.bus.write_byte_data(self.addr, reg, 0)

    def enable_spo2(self) -> None:
        """Drive both LEDs (red + IR); needed for SpO2."""
        self.bus.write_byte_data(self.addr, REG_MODE_CONFIG, MODE_SPO2)

    def read_fifo(self) -> List[Tuple[int, int]]:
        """All samples queued since the last read, oldest first, as (ir, red)."""
        wr = self.bus.read_byte_data(self.addr, REG_FIFO_WR_PTR) & 0x0F
        rd = self.bus.read_byte_data(self.addr, REG_FIFO_RD_PTR) & 0x0F
        overflow = self.bus.read_byte_data(self.addr, REG_OVF_COUNTER)
        n = FIFO_DEPTH if overflow else (wr - rd) & 0x0F
        data = b""
        while n > 0:                 # SMBus block reads are limited to 32 bytes (8 samples)
            chunk = min(n, 8)
            data += bytes(self.bus.read_i2c_block_data(self.addr, REG_FIFO_DATA, chunk * 4))
            n -= chunk
        samples = decode_samples(data)
        if samples:
            self.ir, self.red = samples[-1]
        return samples

    def read_sensor(self) -> None:
        """Update .ir/.red with the newest sample (compatible with the old package)."""
        self.read_fifo()

    def shutdown(self) -> None:
        self.bus.write_byte_data(self.addr, REG_MODE_CONFIG, 0x80)
