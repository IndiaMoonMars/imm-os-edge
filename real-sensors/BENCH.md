# Bench bring-up: every habitat sensor on one Raspberry Pi 5

Wire and verify the sensors one at a time on the desk, on a single Pi 5. None of
the default I2C addresses clash, so everything can share the one bus:

| Address | Device | Driver |
|---|---|---|
| 0x36 | MAX17048 fuel gauge (UPS board) | `bms_driver.py` |
| 0x40 | INA219, main power bus | `power_driver.py` |
| 0x44 | INA219, solar input (A1 bridged) | `bms_driver.py` (`BMS_SOLAR_INA219_ADDRESS=0x44`) |
| 0x48 | ADS1115 #1 (ADDR→GND): ECG | `ecg_driver.py` |
| 0x49 | ADS1115 #2 (ADDR→VDD): O₂ cell | `o2_driver.py` |
| 0x57 | MAX30100 | `biosensor_driver.py` |
| 0x62 | SCD40 | `scd40_driver.py` |
| 0x70 | TCA9548A mux (3 × TSL2561 at 0x39 behind it) | `lux_driver.py` |
| 0x76 | BME280 (SDO→GND) | `bme280_driver.py` |
| UART | STM32 + MQ-7 | `mq7_uart_bridge.py` |
| — | node health | `sysmon_driver.py` |

**Why two ADS1115s:** the ECG and O₂ drivers are separate processes. If they share one
ADS1115, each reconfigures it for its own channel and they read each other's values. With
only one ADS1115, don't run both drivers on the same node.

## Before wiring anything

- **Everything on the header is 3.3 V.** Power the breakouts from pin 1 or 17 (3.3 V).
  Only the MQ-7 heater and STM32 use 5 V (pins 2/4).
- **Power off** while wiring. Add **one sensor at a time**; don't connect the next until
  the current one passes.
- **Pi 5:** use the 27 W (5 V / 5 A) supply and the Active Cooler.
- **I2C pull-ups:** the Pi already has 1.8 kΩ pull-ups, and most breakouts add their own
  10 kΩ. With 8+ boards on the bus the combined pull-up gets too strong. If errors appear
  only with everything connected, open the pull-up jumpers on a few breakouts. Keep I2C
  wires short (< 30 cm), or use Qwiic/STEMMA-QT cables.

Header pins used: **1** 3.3 V · **2/4** 5 V · **3** SDA · **5** SCL · **6/9/14** GND ·
**8** TX · **10** RX.

## Step 0: set up the Pi 5 (no sensors yet)

Flash Raspberry Pi OS Lite 64-bit (Imager: hostname, user, SSH, Wi-Fi), then:

```bash
git clone https://github.com/IndiaMoonMars/imm-os-edge.git && cd imm-os-edge
git checkout claude/github-app-repo-connect-ftvi8y
scp <you>@<mcc-ip>:<path>/imm-os-infra/mosquitto/certs/ca.crt /tmp/ca.crt
sudo ./scripts/setup-node.sh --node-id node-rpi-01 --zone zone_a --mcc-ip <mcc-ip> --ca /tmp/ca.crt
sudo reboot
sudo ./scripts/setup-node.sh --check-only
sudo .venv/bin/python tools/bringup.py            # board, power supply, buses
```

`sysmon_driver.py` is already running. On the MCC, set
`SIM_DISABLED_SENSORS=node-rpi-01:sysmon` in `imm-os-infra/.env` and run
`docker compose up -d sensor-sim`. The Overview's Node health panel shows node-rpi-01 as
**LIVE**: real data through TLS → Kafka → InfluxDB → dashboard, before any sensor is wired.

(The bench Pi uses the habitat node's identity, since most of these sensors belong to
zone A. When the Pi 5 later moves to the compute/power role, re-run setup with
`--node-id node-compute --zone compute`.)

## The sensors, in order

For each one: **wire → `bringup.py <name>` → reference check → next**. The tool checks the
address, identifies the chip where it can, runs the real driver (nothing is published) and
checks every value.

### 1. BME280 (temperature, humidity, pressure)
| BME280 | Pi |
|---|---|
| VIN | pin 1 (3.3 V) |
| GND | pin 6 |
| SDA / SCL | pin 3 / pin 5 |
| SDO | GND → 0x76 (tied high → 0x77: set `BME280_ADDRESS=0x77`) |

`sudo .venv/bin/python tools/bringup.py bme280`. It reads the chip ID: a **BMP280**
(sold as BME280 surprisingly often) has no humidity sensor, and the tool says so.
**Reference check:** a room thermometer (±1 °C) and your city's current pressure (±2 hPa).

### 2. SCD40 (CO₂)
VDD 3.3 V, GND, SDA, SCL → 0x62. `bringup.py scd40` (first reading after ~5 s).
**Reference check:** outdoor air ≈ 420 ppm. Breathe near it and CO₂ should climb within 10 s.

### 3. INA219 (power bus)
VCC 3.3 V, GND, SDA, SCL → 0x40. Put **VIN+ / VIN−** in series with the positive wire of the
load you want to measure (≤ 26 V, e.g. the 12 V habitat bus). `bringup.py ina219`.
**Reference check:** bus voltage against a multimeter (±0.05 V).

### 4. UPS fuel gauge + solar INA219 (the `bms` reading)
The UPS board's MAX17048 shows up at 0x36 once the board is fitted. For solar: a second
INA219 with **A1 bridged → 0x44**, VIN+/VIN− in the panel → charge-controller line. Set
`BMS_SOLAR_INA219_ADDRESS=0x44` in `/etc/imm-os/edge.env`. `bringup.py bms`.
**Reference check:** battery % against the UPS board's LEDs/app.

### 5. TCA9548A + 3 × TSL2561 (light per zone)
Mux: VIN 3.3 V, GND, SDA, SCL, A0–A2 → GND (0x70). TSL2561s on mux channels **0, 1, 2**
(SD0/SC0, SD1/SC1, SD2/SC2), each powered from 3.3 V, ADDR pin floating (0x39).
`bringup.py tsl2561`.
**Reference check:** cover one sensor (lux → ~0), then shine a phone torch on it (thousands of lux).

### 6. O₂ cell on ADS1115 #2
ADS1115: VDD 3.3 V, GND, SDA, SCL, **ADDR → VDD (0x49)**. Cell **+ → A1**, **− → GND**
(the driver reads A1 at ±0.256 V full scale; galvanic cells give ~10–60 mV). First, in
fresh air:
```bash
.venv/bin/python sensor_drivers/o2_driver.py --calibrate   # prints mV → O2_CAL_MV in edge.env
sudo .venv/bin/python tools/bringup.py o2                    # expects 19–23 %
```

### 7. MAX30100 (heart rate, SpO₂)
VIN 3.3 V, GND, SDA, SCL → 0x57. Many purple GY-MAX30100 boards pull SDA/SCL up to
**1.8 V**, which the Pi reads unreliably. If it doesn't show up or drops out, move the
board's pull-ups to 3.3 V (cut the 1.8 V trace / re-solder the jumper) and rely on the Pi's.
`bringup.py max30100`: rest a fingertip on it and stay still for about 10 s (nothing is
published without a finger).
**Reference check:** a clip-on pulse oximeter.

### 8. AD8232 ECG on ADS1115 #1
ADS1115 #1: VDD 3.3 V, GND, SDA, SCL, **ADDR → GND (0x48)**. AD8232: 3.3 V, GND,
**OUTPUT → A0**. Electrodes: RA, LA, RL (right leg).
`bringup.py ecg`.
**Reference check:** with electrodes on, the value moves with each heartbeat.

> ECG streams 100 samples/s by default (`ECG_SAMPLE_HZ`, up to 250), each with its own
> millisecond timestamp. It's the heaviest stream in the pipeline. 100 Hz has been run
> end-to-end through the stack, but the MCC (InfluxDB on the Windows PC) hasn't been
> load-tested above that. Enable it in `IMM_SENSORS` only while you're monitoring someone.

### 9. MQ-7 (CO) via STM32
Build, flash and wire per [firmware/stm32-mq7/README.md](../firmware/stm32-mq7/README.md).
STM32 PA9 → Pi pin 10, PA10 → Pi pin 8, GND ↔ GND. Let it **burn in 24–48 h**, then:
```bash
.venv/bin/python sensor_drivers/mq7_uart_bridge.py --calibrate   # in clean outdoor air
sudo .venv/bin/python tools/bringup.py mq7                        # waits one 150 s cycle
```

## Go live

Once everything has passed, start all the drivers:

```bash
sudo ./scripts/setup-node.sh --sensors "bme280_driver.py scd40_driver.py power_driver.py bms_driver.py lux_driver.py o2_driver.py biosensor_driver.py mq7_uart_bridge.py"
journalctl -u 'imm-sensor-pipeline@*' -f
```

(`sysmon_driver.py` is added automatically. Add `ecg_driver.py` when needed.) On the MCC,
in `imm-os-infra/.env`:

```
SIM_DISABLED_SENSORS=node-rpi-01:bme280,node-rpi-01:scd40,node-rpi-01:o2,node-rpi-01:sysmon
```

then `docker compose up -d sensor-sim`. Every zone A value on the Overview turns
**LIVE**.

**Watch it arrive in real time:** open the mission console's **Sensors** tab. Every
sensor on every node appears as a card as its readings arrive over the realtime link:
- LIVE/SIM badge;
- current values and a trend line (the ECG shows the waveform);
- how often it reports and how long ago it last did.

A card turns amber when a sensor goes quiet for longer than expected, and red when it
has stopped. The node-rpi-02 and node-compute values stay SIM until those Pis exist.

Calibrate against reference instruments afterwards with `tools/calibrate.py`
(see [README.md](README.md#calibration)).

## If something fails

| Symptom | Likely cause |
|---|---|
| Address missing in `bringup.py` | SDA/SCL swapped, no 3.3 V, address jumper; try the device alone |
| Everything works alone, errors together | too many pull-ups (see above), long wires, one faulty board dragging the bus |
| `UNDER-VOLTAGE` | Pi 5 supply too weak or thin USB-C cable; use the 27 W supply |
| MQ-7: no `CO:` lines | not calibrated yet (bring-up shows `stm32: not calibrated`), UART TX/RX crossed |
| O₂ reads far from 20.9 % | `O2_CAL_MV` not set or set in stale air; cell polarity |
| MAX30100 not found / drops out | 1.8 V pull-ups on the module (see step 7) |
