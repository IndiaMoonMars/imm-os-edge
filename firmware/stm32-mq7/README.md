# STM32 MQ-7 controller

An STM32F103C8 "Blue Pill" runs the MQ-7 carbon monoxide sensor's heater cycle and
measures it, then sends CO ppm to the Raspberry Pi over UART
(`sensor_drivers/mq7_uart_bridge.py`).

The MQ-7 only reads CO correctly at the end of a **60 s at 5 V → 90 s at 1.4 V** heater
cycle. The Pi has no ADC and can't hold that timing reliably, so the STM32 does it.

## Parts

| Part | Notes |
|---|---|
| MQ-7 sensor | bare 6-pin sensor, or a module with the heater trace cut (most modules wire the heater straight to 5 V) |
| STM32F103C8 Blue Pill + ST-Link V2 | ST-Link for flashing |
| Logic-level N-MOSFET | AO3400, IRLML2502 or IRLZ44N; switches the ~150 mA heater from 3.3 V logic |
| Resistors | 10 kΩ (RL, load), 100 kΩ + 200 kΩ (divider), 100 Ω (gate), 10 kΩ (gate pull-down) |
| 100 nF capacitor | PA0 to GND (steadies the ADC input from the high-impedance divider) |

## Wiring

```
             5 V (Pi pin 2 or 4)
              │            │
         MQ-7 H1       MQ-7 A (both A pins)
         [heater ~33 Ω]  [sensor Rs]
         MQ-7 H2       MQ-7 B (both B pins) ──┬── RL 10 kΩ ── GND
              │                               │
         MOSFET drain                        100 kΩ
         MOSFET source ── GND                 │
         MOSFET gate ── 100 Ω ── PA8          ├── PA0   (+100 nF to GND)
                     └── 10 kΩ ── GND        200 kΩ
                                              │
                                             GND

  STM32 PA9  (TX) ──→ Pi pin 10 (RX)
  STM32 PA10 (RX) ←── Pi pin 8  (TX)
  STM32 GND ─────── Pi GND        STM32 5V pin ← Pi 5 V (or power it from USB)
```

All grounds are common. The STM32 pins are 3.3 V logic, like the Pi's, so no level
shifter is needed on the UART. The divider keeps PA0 below 3.3 V, and the firmware
allows for the divider's load in parallel with RL.

## Build and flash

The firmware is `src/main.cpp`, built with PlatformIO (installs the STM32 toolchain on first use):

```bash
pip install platformio
pio run -d firmware/stm32-mq7                 # build
pio run -d firmware/stm32-mq7 -t upload       # flash via ST-Link (SWDIO, SWCLK, GND, 3.3 V)
pio device monitor -b 115200                  # optional: watch the output over a USB-serial adapter
```

With the Arduino IDE instead: install the "STM32 MCU based boards" (STM32duino) core, then
choose Generic STM32F1 series → BluePill F103C8. Rename `src/main.cpp` to
`stm32-mq7.ino` in a folder of the same name.

> This firmware has been compiled and run on a PC against a simulated clock, ADC and UART
> (`test/sim.cpp`, run by `tests/test_mq7.py`). That covers the heater timing, PWM duty,
> CO maths, calibration and flash storage. It has **not yet been compiled against the STM32
> core**, because the PlatformIO registry was unreachable where it was written. The first
> `pio run` on your PC is that check.

## Output (115200 8N1, one CO line per 150 s cycle)

```
# IMM-OS MQ-7 controller started (60 s @ 5 V, 90 s @ 1.4 V)
# vout=1.333 rs=27501 r0=1000 ratio=27.50 cycle=1
CO:0.6
```

`#` lines are diagnostics: the bridge shows them but doesn't publish them.

## First use

1. **Burn in:** leave it running for 24–48 h. A new MQ-7's readings drift heavily at first.
2. **Calibrate** in clean outdoor air (this sets R0 = Rs ÷ 27.5 and stores it in the STM32's flash):
   ```bash
   sudo systemctl stop imm-sensor-pipeline@mq7_uart_bridge.py   # if already running
   .venv/bin/python sensor_drivers/mq7_uart_bridge.py --calibrate
   ```
3. **Check:** `sudo .venv/bin/python tools/bringup.py mq7` (waits for one full cycle).

Treat the ppm values as indicative: an MQ-7 without a calibration gas is good for
trends and alarms (the backend alarms above 35 ppm), not for exact figures.
