"""
Minimal MFRC522 (RC522) RFID reader driver over SPI, using spidev only.

Replaces the `mfrc522` package, which drives the reset pin through RPi.GPIO and so
cannot run on a Raspberry Pi 5. This driver needs no GPIO: tie the module's RST pin
to 3.3 V and it is reset over SPI instead.

Wiring (SPI0, CE0): SDA→GPIO8 (pin 24), SCK→GPIO11 (23), MOSI→GPIO10 (19),
MISO→GPIO9 (21), RST→3.3 V (pin 17), GND, 3.3 V (never 5 V).

Only what tag scanning needs: REQA + anticollision, returning the 4-byte UID
(cascade level 1; 7-byte UIDs report their first 3 bytes after the 0x88 cascade tag,
the same as the old library).
"""
import time
from typing import List, Optional, Tuple

# Registers
COMMAND = 0x01
COM_IEN = 0x02
COM_IRQ = 0x04
ERROR = 0x06
FIFO_DATA = 0x09
FIFO_LEVEL = 0x0A
CONTROL = 0x0C
BIT_FRAMING = 0x0D
MODE = 0x11
TX_CONTROL = 0x14
TX_ASK = 0x15
T_MODE = 0x2A
T_PRESCALER = 0x2B
T_RELOAD_H = 0x2C
T_RELOAD_L = 0x2D
VERSION = 0x37

# Commands
CMD_IDLE = 0x00
CMD_TRANSCEIVE = 0x0C
CMD_SOFT_RESET = 0x0F

PICC_REQA = 0x26
PICC_ANTICOLL_CL1 = 0x93

KNOWN_VERSIONS = {0x88: "FM17522 clone", 0x90: "MFRC522 v0.0", 0x91: "MFRC522 v1.0",
                  0x92: "MFRC522 v2.0", 0xB2: "FM17522 clone", 0x12: "counterfeit MFRC522"}


class RC522:
    def __init__(self, bus: int = 0, device: int = 0, spi=None, speed_hz: int = 1_000_000):
        if spi is None:
            import spidev
            spi = spidev.SpiDev()
            spi.open(bus, device)
            spi.max_speed_hz = speed_hz
            spi.mode = 0
        self.spi = spi
        self.reset()

    # ── register access ──
    def write(self, reg: int, value: int) -> None:
        self.spi.xfer2([(reg << 1) & 0x7E, value & 0xFF])

    def read(self, reg: int) -> int:
        return self.spi.xfer2([((reg << 1) & 0x7E) | 0x80, 0])[1]

    def set_bits(self, reg: int, mask: int) -> None:
        self.write(reg, self.read(reg) | mask)

    def clear_bits(self, reg: int, mask: int) -> None:
        self.write(reg, self.read(reg) & ~mask)

    # ── setup ──
    def version(self) -> int:
        return self.read(VERSION)

    def reset(self) -> None:
        self.write(COMMAND, CMD_SOFT_RESET)
        time.sleep(0.05)
        self.write(T_MODE, 0x8D)          # timer starts after each transmission
        self.write(T_PRESCALER, 0x3E)     # ~25 ms timeout with the reload below
        self.write(T_RELOAD_L, 30)
        self.write(T_RELOAD_H, 0)
        self.write(TX_ASK, 0x40)          # 100 % ASK modulation
        self.write(MODE, 0x3D)            # CRC preset 0x6363
        if (self.read(TX_CONTROL) & 0x03) != 0x03:
            self.set_bits(TX_CONTROL, 0x03)   # antenna on

    # ── card commands ──
    def transceive(self, data: List[int], tx_last_bits: int = 0, timeout_s: float = 0.05) -> Tuple[Optional[List[int]], int]:
        """Send data to the card; returns (response bytes or None, valid bits in the last byte)."""
        self.write(COM_IEN, 0x77 | 0x80)
        self.clear_bits(COM_IRQ, 0x80)    # clear all interrupt request bits
        self.set_bits(FIFO_LEVEL, 0x80)   # flush FIFO
        self.write(COMMAND, CMD_IDLE)
        for b in data:
            self.write(FIFO_DATA, b)
        self.write(BIT_FRAMING, tx_last_bits & 0x07)
        self.write(COMMAND, CMD_TRANSCEIVE)
        self.set_bits(BIT_FRAMING, 0x80)  # StartSend
        deadline = time.monotonic() + timeout_s
        while True:
            irq = self.read(COM_IRQ)
            if irq & 0x30:                # RxIRq or IdleIRq: done
                break
            if irq & 0x01 or time.monotonic() > deadline:   # timer: no card answered
                self.clear_bits(BIT_FRAMING, 0x80)
                return None, 0
        self.clear_bits(BIT_FRAMING, 0x80)
        if self.read(ERROR) & 0x1B:       # buffer overflow, collision, parity, protocol
            return None, 0
        n = self.read(FIFO_LEVEL) & 0x7F
        last_bits = self.read(CONTROL) & 0x07
        return [self.read(FIFO_DATA) for _ in range(n)], last_bits

    def request(self) -> bool:
        """REQA: True when a card is in the field."""
        resp, bits = self.transceive([PICC_REQA], tx_last_bits=7)
        return resp is not None and len(resp) == 2 and bits == 0

    def anticollision(self) -> Optional[List[int]]:
        resp, _ = self.transceive([PICC_ANTICOLL_CL1, 0x20])
        if not resp or len(resp) != 5:
            return None
        bcc = resp[0] ^ resp[1] ^ resp[2] ^ resp[3]
        return resp[:4] if bcc == resp[4] else None

    def read_uid(self) -> Optional[str]:
        """UID of the card in the field as hex (e.g. 'DEADBEEF'), or None."""
        if not self.request():
            return None
        uid = self.anticollision()
        return "".join(f"{b:02X}" for b in uid) if uid else None
