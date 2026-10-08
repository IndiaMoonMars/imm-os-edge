"""
Minimal INA219 current/power monitor driver (I2C, smbus2).

Replaces pi-ina219, whose Adafruit-GPIO dependency has no wheels and no longer builds
on current Raspberry Pi OS (Python 3.13). Same interface as pi-ina219 for what the
drivers use: INA219(shunt_ohms, busnum=, address=), configure(), voltage() in V,
current() in mA, power() in mW, and DeviceRangeError.

Configuration: 32 V bus range, ±320 mV shunt range (PGA /8), 12-bit conversions
averaged over 8 samples (4.26 ms each), shunt and bus measured continuously.
Current is computed from the shunt voltage (10 µV steps) and the shunt resistance,
so the calibration register isn't needed: ±3.2 A in 0.1 mA steps with the usual
0.1 Ω shunt.
"""

REG_CONFIG = 0x00
REG_SHUNT_VOLTAGE = 0x01
REG_BUS_VOLTAGE = 0x02

CONFIG_RESET = 0x8000
BRNG_32V = 1 << 13
PGA_320MV = 0b11 << 11
ADC_12BIT_8S = 0b1011            # 8-sample average, 4.26 ms
MODE_SHUNT_BUS_CONT = 0b111
CONFIG = BRNG_32V | PGA_320MV | ADC_12BIT_8S << 7 | ADC_12BIT_8S << 3 | MODE_SHUNT_BUS_CONT

SHUNT_LSB_MV = 0.01              # 10 µV
BUS_LSB_V = 0.004                # 4 mV
SHUNT_FULL_SCALE = 32000         # ±320 mV at PGA /8


class DeviceRangeError(Exception):
    """The current exceeds what the shunt range can measure."""


def to_signed(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


class INA219:
    def __init__(self, shunt_ohms: float, max_expected_amps=None, busnum: int = 1, address: int = 0x40, bus=None):
        if shunt_ohms <= 0:
            raise ValueError("shunt_ohms must be positive")
        if bus is None:
            from smbus2 import SMBus
            bus = SMBus(busnum)
        self.bus, self.address, self.shunt_ohms = bus, address, shunt_ohms

    # registers are 16-bit big-endian; SMBus word calls are little-endian, so use 2-byte blocks
    def _read(self, reg: int) -> int:
        hi, lo = self.bus.read_i2c_block_data(self.address, reg, 2)
        return (hi << 8) | lo

    def _write(self, reg: int, value: int):
        self.bus.write_i2c_block_data(self.address, reg, [(value >> 8) & 0xFF, value & 0xFF])

    def configure(self, *args, **kwargs):
        """Reset, then set the ranges above (pi-ina219's arguments are accepted and ignored)."""
        self._write(REG_CONFIG, CONFIG_RESET)
        self._write(REG_CONFIG, CONFIG)

    def shunt_voltage(self) -> float:
        """mV across the shunt; raises DeviceRangeError at full scale."""
        raw = to_signed(self._read(REG_SHUNT_VOLTAGE))
        if abs(raw) >= SHUNT_FULL_SCALE:
            raise DeviceRangeError(f"shunt voltage at full scale ({raw * SHUNT_LSB_MV:.1f} mV)")
        return raw * SHUNT_LSB_MV

    def voltage(self) -> float:
        """Bus voltage in V (IN- to GND); raises DeviceRangeError on math overflow."""
        raw = self._read(REG_BUS_VOLTAGE)
        if raw & 0x0001:
            raise DeviceRangeError("INA219 math overflow: current beyond the shunt range")
        return (raw >> 3) * BUS_LSB_V

    def supply_voltage(self) -> float:
        """Voltage on IN+ (bus + shunt drop) in V."""
        return self.voltage() + self.shunt_voltage() / 1000.0

    def current(self) -> float:
        """mA through the shunt (negative when current flows IN- → IN+)."""
        return self.shunt_voltage() / self.shunt_ohms

    def power(self) -> float:
        """mW delivered to the load."""
        return self.voltage() * self.current()
