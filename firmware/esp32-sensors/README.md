# ESP32 sensor board

The ESP32 board reads its five sensors and sends one JSON line per second over USB to the
Raspberry Pi. On the Pi, `sensor_drivers/esp32_bridge.py` publishes each sensor to IMM-OS
as its own LIVE stream. The Pi handles TLS, login and the offline blackbox, so the board
needs no Wi-Fi and stores no passwords.

```
 ESP32 board ──USB (power + data)──► Pi 5: esp32_bridge.py ──MQTT/TLS──► MCC dashboard
```

| Sensor | Connection | Published as |
|---|---|---|
| BME280 | I2C 0x76/0x77 | `bme280`: temp, hum, pres (same cards as a Pi-wired BME280) |
| SCD40 | I2C 0x62 | `scd40`: co2_ppm, temp, hum (a new reading every 5 s) |
| BNO055 | I2C 0x28/0x29 | `bno055`: heading, roll, pitch, linear acceleration, calibration 0–3 |
| DFRobot SEN0322 | I2C 0x70–0x73 | `o2`: o2_pct |
| MQ-4 | AO → divider → GPIO32 | `mq4`: ch4_ppm, rs_r0 (after warm-up and calibration) |

I2C is on GPIO21 (SDA) and GPIO22 (SCL), with the four I2C sensors powered from 3.3 V. The
MQ-4 runs on 5 V.

**The MQ-4 must go through a divider.** Its AO pin can rise toward 5 V, but the ESP32 only
tolerates about 3.6 V. The board has 10 kΩ from AO to GPIO32 and 10 kΩ from GPIO32 to GND
(scale ×2.0, so 5 V at AO is 2.5 V at the pin). If your resistors differ, set `MQ4_DIVIDER` in `platformio.ini` to
(top + bottom) / bottom.

## Flash it (Windows)

1. Install [VS Code](https://code.visualstudio.com/), then the **PlatformIO IDE** extension.
2. Plug the ESP32 into the PC with a USB **data** cable. If Windows doesn't show a COM
   port, install the USB driver for the board's chip: CP210x (Silicon Labs) or CH340.
3. Open a PlatformIO terminal (the terminal icon in the PlatformIO toolbar) and run:
   ```
   cd C:\Users\PRATHAM\Documents\imm-os-edge
   pio run -d firmware/esp32-sensors -t upload
   pio device monitor -b 115200
   ```
   If the upload waits at `Connecting....`, hold the board's **BOOT** button until it starts.
4. The monitor shows `# IMM-OS ESP32 sensor board started`, a `# sensors:` line listing
   what it found, then one `{...}` line per second. Press Ctrl+C to quit.

This replaces the board's previous Wi-Fi dashboard firmware. Keep a copy of that sketch
if you want to go back to it.

## Connect it to the Pi

Unplug the board's adapter, and plug the board into a USB port on the Pi instead. The Pi
powers it (well within the Pi 5's USB budget with the 27 W supply) and reads it over the
same cable. Then, on the PC:

```powershell
ssh -t pratham@<pi-ip> "cd imm-os-edge && sudo .venv/bin/python tools/bringup.py esp32"
```

## Calibration

Send these commands with `pio device monitor` on the PC, or from the Pi with
`sensor_drivers/esp32_bridge.py --send CAL_MQ4`.

- **MQ-4 (methane):**
  - Give a new sensor 24–48 h of burn-in first; after every power-up it also needs 3 minutes to warm up.
  - Then, in clean outdoor air, send `CAL_MQ4`. It averages 10 s of readings and stores R0 in flash, where it survives power-off.
  - Until then, the board reports only the raw voltage.
  - ppm uses the datasheet curve for methane (ppm = 1012.7 × (Rs/R0)^−2.786, with Rs/R0 = 4.4 in clean air). Treat it as an estimate within the sensor's 200–10,000 ppm range, and check it against a reference gas detector.
- **SEN0322 (oxygen):** in fresh outdoor air, send `CAL_O2`. The sensor then treats that reading as 20.9 %.
- **BNO055:** it calibrates itself as it moves. `imm_calib` goes from 0 to 3, and 3 means fully calibrated. Rotate the board slowly through a few orientations to get there.
- `STATUS` lists which sensors were found and the MQ-4 calibration.

## Test on a PC (no hardware)

`tests/test_esp32_firmware.py` compiles `src/main.cpp` with the stand-ins in `test/stub/`
and runs it against simulated sensors (`test/sim.cpp`). It checks:
- register decoding for every sensor;
- the BME280 math, against Bosch's floating-point formulas;
- the SCD40's 5 s cadence;
- a missing sensor being left out and found again;
- the MQ-4 and O₂ calibration flows, including R0 surviving a reboot.
