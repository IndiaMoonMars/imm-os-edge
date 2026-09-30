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
| BME280 | I2C 0x76/0x77 | `bme280`: temp, hum, pres, dew_point_c (same cards as a Pi-wired BME280) |
| SCD40 | I2C 0x62 | `scd40`: co2_ppm, temp, hum, dew_point_c (a new reading every 5 s) |
| BNO055 | I2C 0x28/0x29 | `bno055`: heading, roll, pitch, linear acceleration, rotation rate, gravity, magnetic field, chip temperature, calibration 0–3 (system, gyro, accelerometer, magnetometer) |
| DFRobot SEN0322 | I2C 0x70–0x73 | `o2`: o2_pct |
| MQ-4 | AO → divider → GPIO32 | `mq4`: vout_mv, rs_rl, warming, calibrated; rs_r0 and ch4_ppm after warm-up and calibration |

dew_point_c is worked out on the Pi from temp and hum. The BNO055's power-on self-test
(accelerometer, magnetometer, gyro, MCU) is printed with `STATUS` and at start-up.

What the gas sensors can see: the SCD40 measures only CO₂ (infrared, 400–5000 ppm); the
SEN0322 only O₂; the MQ-4 reacts most to methane but also to LPG, hydrogen, alcohol and
smoke, without telling them apart, and reads 200–10000 ppm (below that, read it as "no
methane").

I2C is on GPIO21 (SDA) and GPIO22 (SCL), with the four I2C sensors powered from 3.3 V. The
MQ-4 runs on 5 V.

**The MQ-4 must go through a divider.** Its AO pin can rise toward 5 V, but the ESP32 only
tolerates about 3.6 V. The board has 10 kΩ from AO to GPIO32 and 10 kΩ from GPIO32 to GND
(scale ×2.0, so 5 V at AO is 2.5 V at the pin). If your resistors differ, set `MQ4_DIVIDER` in `platformio.ini` to
(top + bottom) / bottom.

## Flash it from the Pi (no laptop USB needed)

Plug the board into a Pi USB port (USB-A to the board's connector, a **data** cable). Then,
in PowerShell on the MCC PC:

```powershell
cd C:\Users\PRATHAM\Documents\imm-os-edge
git pull origin claude/github-app-repo-connect-ftvi8y
powershell -ExecutionPolicy Bypass -File .\scripts\provision-pi.ps1 -PiUser pratham -PiHost <pi-ip> -CopyOnly
ssh -t pratham@<pi-ip> "cd imm-os-edge && ./scripts/flash-esp32.sh"
```

`-CopyOnly` copies the latest code to the Pi and nothing else. `flash-esp32.sh`:
- installs PlatformIO on the Pi the first time (a few minutes; the first build also downloads the ESP32 toolchain);
- builds and flashes the board;
- shows its first 15 s of output.

If the upload waits at `Connecting....`, hold the board's **BOOT** button until it starts.

## Flash it from a Windows PC

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

Keep the board on its 12 V adapter (12 V → buck → 5 V rail → ESP32 VIN and MQ-4) and use
the USB cable to the Pi for data. Only plug both in if the ESP32 board has a diode between
its USB 5 V and its VIN pin (check below); otherwise the buck's 5 V and the Pi's USB 5 V are
tied together.

**Do not run the board from the Pi's USB alone.** USB 5 V reaches VIN through that diode
at about 4.5 V once the MQ-4 heater (~150 mA) is on. The ESP32 board's 3.3 V regulator
(AMS1117 class, ~1.1 V dropout) then has almost no headroom, and every SCD40 measurement
(up to ~200 mA on 3.3 V every 5 s) dips the 3.3 V rail. On the first board this reset the
BME280 dozens of times a minute (`bme280: chip reset N time(s)`) and the SCD40 reported
CO₂ 0. The MQ-4 heater also wants 5.0 V ± 0.1 V, so calibrate it on the adapter only.

Checking for the diode, with everything unplugged: multimeter in diode mode, the USB cable
plugged into the ESP32 board only. Probe between the free USB-A plug's VBUS pin (pin 1, an
outer pin) and the board's VIN/5V pin, both ways round. A diode reads about 0.2–0.4 V one
way and open (OL) the other way. A beep or 0.00 V both ways means no diode: then power the
board from USB alone only for flashing, not for measuring.

Then, on the PC:

```powershell
ssh -t pratham@<pi-ip> "cd imm-os-edge && sudo .venv/bin/python tools/bringup.py esp32"
```

## Check every value

On the Pi (it pauses the esp32_bridge service while it runs):

```bash
cd ~/imm-os-edge && sudo .venv/bin/python tools/verify_esp32.py
```

It lists every value, checks each against physics and the sensors against each other (the
BME280's and SCD40's dew points, O₂ against CO₂, gravity 9.8 m/s², the Earth's magnetic
field, the BNO055 self-test, the MQ-4 signal), then guides hands-on tests: breathe on the
board (CO₂, humidity, O₂), warm the BME280 with your hand, tilt and turn the board (BNO055),
gas from an unlit lighter (MQ-4). `--auto` skips the hands-on part. The IMM-OS Sensors tab
shows the same automatic checks live under each node.

## Wi-Fi (optional)

The board can also send its readings over Wi-Fi. USB output continues, and the MQ-4 is on ADC1,
which works with Wi-Fi on. Set it up once over USB. The network name and password are stored in
the board's flash, never in code:

```bash
cd ~/imm-os-edge
.venv/bin/python sensor_drivers/esp32_bridge.py --send "WIFI_SSID <network name>"
.venv/bin/python sensor_drivers/esp32_bridge.py --send "WIFI_PASS <password>"
.venv/bin/python sensor_drivers/esp32_bridge.py --send STATUS      # "wifi: connected ip=192.168.1.x"
```

Then `http://<board-ip>/` shows a live page, and `http://<board-ip>/json` serves the same line as USB.
Give the board a DHCP reservation in the router, and point the Pi at it (from the MCC PC):
`.\scripts\provision-pi.ps1 … -IntBoard http://<board-ip>/json`, or `ESP32_URL=` in `/etc/imm-os/edge.env`.
Commands such as `CAL_CO2` still go over USB. `WIFI_OFF` forgets the network. Wi-Fi draws current
peaks of about 300 mA: if `board.reset_reason` shows 9 (brownout), use a better 5 V supply or cable.

## Calibration

Send these commands with `pio device monitor` on the PC, or from the Pi with
`sensor_drivers/esp32_bridge.py --send CAL_MQ4` (quote commands with an argument: `--send "CAL_CO2 430"`).
Before a mission, do them in the order given in imm-os-docs `mission-operations.md`.

- **MQ-4 (methane):**
  - Give a new sensor 24–48 h of burn-in first. After a real power-on (or a brownout) it needs 3 minutes to warm up; the board reports `warm_left_s` for the countdown. After a watchdog, crash, software or EN-button reset the heater kept its power, so there is no warm-up.
  - Then, in clean outdoor air, send `CAL_MQ4`. It averages 10 s of readings and stores R0 in flash, where it survives power-off.
  - Until then, the board reports only the raw voltage.
  - ppm uses the datasheet curve for methane (ppm = 1012.7 × (Rs/R0)^−2.786, with Rs/R0 = 4.4 in clean air). Treat it as an estimate within the sensor's 200–10,000 ppm range, and check it against a reference gas detector.
- **SEN0322 (oxygen):** in fresh outdoor air, send `CAL_O2`. The sensor then treats that reading as 20.9 %.
- **SCD40 (CO₂):** after 3 min in fresh outdoor air, send `CAL_CO2` (or `CAL_CO2 430` for a known level). This is a forced recalibration to 420 ppm, and it turns the sensor's automatic self-calibration off. That self-calibration needs about 7 days of regular fresh air, as long as a mission, and would drift instead. Both are stored in the sensor; the board reports `asc` (1 on, 0 off). `ASC_ON` turns it back on after the mission.
- **BNO055:** it calibrates itself as it moves. `imu_calib` (and `calib_gyro`, `calib_acc`, `calib_mag`) go from 0 to 3, and 3 means fully calibrated. Rotate the board slowly through a few orientations to get there. Once all four read 3, the board stores the calibration offsets in flash and writes them back at every start (`cal_restored: 1`), so a restart doesn't lose it. `CAL_BNO_CLEAR` forgets them.
- `STATUS` lists which sensors were found and the MQ-4 calibration.

## Test on a PC (no hardware)

`tests/test_esp32_firmware.py` compiles `src/main.cpp` with the stand-ins in `test/stub/`
and runs it against simulated sensors (`test/sim.cpp`). It checks:
- register decoding for every sensor;
- the BME280 math, against Bosch's floating-point formulas;
- the SCD40's 5 s cadence;
- a missing sensor being left out and found again;
- the MQ-4 and O₂ calibration flows, including R0 surviving a reboot.
