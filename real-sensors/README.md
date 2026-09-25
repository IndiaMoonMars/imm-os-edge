# 🛰️ Real Sensor Integration Guide

Real sensors and the simulator publish **the same readings on the same MQTT topics**,
so moving from simulated to real data needs no backend changes. Each reading
carries `"simulated": true/false` and the dashboard shows a **LIVE** or **SIM**
badge for every value.

---

## How the data flows

```
[sensor_drivers/*.py on Raspberry Pi 4/5]    [sensor-sim container]
            │  habitat/sensors/<sensor>/<zone>   (MQTT over TLS, user imm-edge)
            ▼
     [Mosquitto] ──▶ [mqtt-kafka-bridge] ──▶ Kafka telemetry.raw
                                                  │
                                    [telemetry-validator]  ── rejects ──▶ telemetry.deadletter
                                                  ▼
                                        Kafka telemetry.validated
                             ┌────────────────────┼─────────────────────┐
                             ▼                    ▼                     ▼
                 [telemetry-processor]   [telemetry-ingest WS]    [ai-processor]
                 InfluxDB habitat_sensors     OpenMCT live          anomaly model
                 + habitat_alerts + PG alarms
                             │
                             ▼
             backend /api/telemetry/latest ──▶ Mission Overview / Life Support
```

## Reading format

One JSON object per reading. `node_id`, `zone` and `simulated` are added
automatically by `core/mqtt_publisher.py` (from `IMM_NODE_ID` / `IMM_ZONE`).

```json
{ "sensor": "bme280", "temp": 22.5, "hum": 48.1, "pres": 1012.3,
  "timestamp": 1790330000, "node_id": "node-rpi-01", "zone": "zone_a", "simulated": false }
```

Topic: `habitat/sensors/<sensor>/<zone>`. The sensor in the topic must match the
payload, or the validator rejects the reading.

| Sensor (`sensor`) | Fields | Driver |
|---|---|---|
| `bme280` | `temp` °C, `hum` %, `pres` hPa | `sensor_drivers/bme280_driver.py` |
| `scd40` | `co2_ppm`, `temp`, `hum` | `scd40_driver.py` |
| `o2` | `o2_pct` | `o2_driver.py` (needs `O2_CAL_MV`) |
| `mq7` | `co_ppm` | `mq7_uart_bridge.py` (STM32 over UART) |
| `max30100` | `hr_bpm`, `spo2_pct` | `biosensor_driver.py` |
| `ecg_ad8232` | `voltage` | `ecg_driver.py` |
| `tsl2561` | `lux` (per zone) | `lux_driver.py` |
| `ina219` | `voltage_v`, `current_ma`, `power_mw` | `power_driver.py` |
| `sysmon` | `cpu_temp`, `cpu_load`, `mem_pct`, `disk_pct`, `fan_rpm`, `power_w`, `supply_v`, `undervolt`, `throttled` | `sysmon_driver.py` (every node) |
| `bms` | `battery_pct`, `solar_w` | `bms_driver.py` (MAX17048 gauge + solar INA219) |
| `jetson` | `cpu_temp`, `gpu_temp`, `power_w` | `jetson_driver.py` (only if a Jetson is added later) |

The schema lives in `imm-os-backend/services/telemetry_schema.py`.

---

## Bringing a sensor online

1. **Set up the node** with `scripts/setup-node.sh` (see the top-level README): packages,
   interfaces, `/etc/imm-os/edge.env` (`IMM_NODE_ID`, `IMM_ZONE`, MQTT password, CA),
   calibration file and services, then connectivity checks.
2. **Bench-test with the bring-up tool**. It checks the board and bus (power supply,
   I2C address, UART, serial console), runs the real driver in stdout mode (nothing is
   sent) and checks every value against a plausible range:
   ```bash
   sudo .venv/bin/python tools/bringup.py            # board + all buses
   sudo .venv/bin/python tools/bringup.py bme280     # one sensor, 3 readings
   sudo .venv/bin/python tools/bringup.py --list
   ```
   Then do its reference check (it prints one, e.g. breathe on the SCD40).
3. **Go live.** Add the driver to the node's list and re-run setup; the systemd unit
   runs it with `--mode both` (MQTT plus the local encrypted blackbox):
   ```bash
   sudo ./scripts/setup-node.sh --sensors "bme280_driver.py scd40_driver.py"
   journalctl -u imm-sensor-pipeline@bme280_driver.py -f
   ```
   Only the listed drivers run; nothing is started for hardware a node doesn't have.
4. **Calibrate** against a reference instrument (see "Calibration" below).
5. **Stop simulating that sensor** on the MCC. In `imm-os-infra/.env`:
   ```
   SIM_DISABLED_SENSORS=node-rpi-01:bme280
   ```
   then `docker compose up -d sensor-sim`. A bare `bme280` stops it on every
   simulated node. When everything is real: `docker compose stop sensor-sim`.
6. **Check** the Overview page: the value shows **LIVE**. If it doesn't, look for
   rejected readings:
   ```bash
   docker compose logs telemetry-validator --tail 20
   ```

---

## Calibration

Each node keeps its corrections in `/etc/imm-os/calibration.yaml`
(`corrected = raw × gain + offset`), applied to every reading before it is published
or written to the blackbox. Edit it with `tools/calibrate.py`, which validates entries
and records the reference, date and who calibrated:

```bash
IMM_CALIBRATION_OFF=1 .venv/bin/python sensor_drivers/bme280_driver.py     # raw values
sudo .venv/bin/python tools/calibrate.py one-point bme280.temp --raw 23.6 --true 22.8 --reference "Testo 605i"
sudo .venv/bin/python tools/calibrate.py two-point bme280.hum --raw 31.0,72.9 --true 33.0,75.3 --reference "salt jars 33/75 %"
sudo .venv/bin/python tools/calibrate.py report      # Markdown sign-off sheet for the acceptance checklist
```

| Sensor | Reference method |
|---|---|
| temperature (bme280/scd40/ds18b20) | calibrated thermometer beside it, 30 min settle — one-point |
| humidity | saturated salt jars: MgCl₂ ≈ 33 %, NaCl ≈ 75 % — two-point |
| CO₂ (scd40) | outdoor air ≈ 420 ppm — one-point |
| O₂ | `o2_driver.py --calibrate` in fresh air (sets `O2_CAL_MV`), then verify |
| pH (ezo_ph) | EZO mid/low/high buffer calibration first; calibration.yaml only for residual offset |
| waste scale | `waste_tracker.py --calibrate 1.0` (sets `HX711_SCALE`) |

Changes take effect on the next reading. A malformed file is ignored (the previous
good calibration stays in force) and logged.

## Templates (older format)

`templates/rpi-sensor-driver.py` and `templates/jetson-sensor-driver.py` publish the
older per-node format (`imm/habitat/<node>/telemetry/<measurement>`). The dashboard
still reads it as a fallback, but it skips validation, alerts and OpenMCT, so
prefer the drivers in `sensor_drivers/`.

## Troubleshooting

| Problem | Check |
|---|---|
| Value still shows **SIM** | Is that sensor in `SIM_DISABLED_SENSORS`? Is the driver running (`journalctl`)? |
| Nothing arrives | `docker compose logs mqtt-kafka-bridge` and `mosquitto`; MQTT password and CA on the node |
| Readings rejected | `docker compose logs telemetry-validator` shows the reason (bad field, wrong topic, clock off by more than 7 days) |
| Values wrong | Calibration (`O2_CAL_MV`), sensor burn-in (MQ-7 needs 24–48 h) |
| Driver can't find the device | `tools/bringup.py <sensor>`: shows what answers on I2C and which UART is used |
| `undervolt` = 1 / random resets | Power supply too weak. Pi 5: the official 27 W (5 V / 5 A) supply |

## Raspberry Pi 5 notes

All drivers run on a Pi 4 or a Pi 5 (64-bit Raspberry Pi OS). The Pi 5's RP1 I/O chip
changes three things, all handled in the code:

- **GPIO:** `RPi.GPIO` does not work on a Pi 5. GPIO goes through gpiozero/lgpio, and the
  RC522 RFID reader uses `core/rc522.py` (SPI only, RST tied to 3.3 V) instead of the
  `mfrc522` package.
- **I2C for Adafruit drivers:** they open the bus by number (`core/hw.py: i2c_bus()`,
  `I2C_BUS`) instead of `import board`, which on a Pi 5 needs libgpiod bindings.
- **UART:** the header UART (GPIO14/15, pins 8/10) is `/dev/ttyAMA0`, enabled with
  `dtparam=uart0=on` (`setup-node.sh` adds it). `/dev/serial0` is the separate debug
  connector on a Pi 5. `GPS_PORT` / `MQ7_PORT` default to the right device.

The compute/power node is a Pi 5 (`node-compute`): `sysmon_driver.py` reports its
temperature, PMIC power, fan and supply state, and `bms_driver.py` its UPS battery
and solar input. Use the active cooler and the 27 W supply.

---

## ECLSS and EVA hardware (Phase 5)

Every script in `eclss/` and `eva/` drives real hardware by default. Add
`--simulate` (or `IMM_SIMULATE=true` in edge.env) to run the old synthetic
behaviour without hardware. Settings for all of them are in
`systemd/edge.env.example`.

`scripts/setup-node.sh` enables I2C, SPI, the UART and 1-Wire (DS18B20 on GPIO4) and
adds the service user to the gpio, i2c, spi, dialout and input groups; reboot once
afterwards if it asks.

| Script | Hardware | Sends | Setup / calibration |
|---|---|---|---|
| `eclss/water_monitor.py` | YF-S201 flow meter, GPIO17 (5 V signal → divider) | one event per draw + daily total → ECLSS API | `FLOW_PULSES_PER_L` (450 nominal; check with a measuring jug) |
| `eclss/shower_timer.py` | HC-SR501 PIR, GPIO27 | shower duration + estimated litres | set the PIR hold-time pot to minimum; `SHOWER_LPM` for your shower head |
| `eclss/waste_tracker.py` | HX711 + load cell (DOUT 5, SCK 6) + RFID reader | settled weight increase + scanned waste-type tag | `--calibrate 1.0` with a 1 kg reference → `HX711_SCALE` |
| `eclss/biolab_monitor.py` | Atlas EZO-pH (I2C 0x63) + DS18B20 (1-Wire) | temperature-compensated pH + water temp | calibrate the probe with the EZO's mid/low/high buffer commands |
| `eclss/eclss_pid.py` | 2 relays: HVAC GPIO23, dehumidifier GPIO24 | switches loads from this node's BME280/SCD40 readings | setpoints in edge.env; relays OFF if readings stop (fail-safe) |
| `eclss/lighting_controller.py --listen` | PCA9685 + warm/cool white LED strips | follows lighting commands from the crew app | `LIGHT_ZONES="core:0,1;…"` (warm, cool channel per zone) |
| `eva/gps_driver.py` | u-blox NEO-M9N on the UART (38400) | fixes → `habitat/eva/gps` | open sky; the first fix can take a minute |
| `eva/uwb_driver.py` | DWM1001 tag (USB `/dev/ttyACM0`) + 4 anchors | x/y/z + quality → `habitat/eva/uwb` | set anchor positions with the Decawave app first |
| `eva/position_fusion.py` | — | best of UWB/GPS → `habitat/eva/position/<crew>` (OpenMCT EVA tracker) | `UWB_QUALITY_THRESH` |
| `eva/eva_biosensor_driver.py` | MAX30100, MLX90614, AD8232+ADS1115 | HR, SpO2, skin temp, ECG at 5 Hz | `CREW_ID` = the wearer's IMM-OS username |
| `eva/tool_tracker.py` | USB or RC522 RFID reader at the airlock | CHECKOUT/CHECKIN per scan | tools listed in `TOOLS_FILE` (CSV) |

Run them as services by listing them when setting up the node:
```bash
sudo ./scripts/setup-node.sh --eclss "water_monitor waste_tracker eclss_pid" \
     --eva "gps_driver uwb_driver position_fusion eva_biosensor_driver"
journalctl -u imm-eclss@water_monitor -f
```

Events posted to the ECLSS/EVA APIs while the MCC is unreachable are kept in
`/var/lib/imm-os/spool/` and delivered, oldest first, once it is back.

Heart rate and SpO2 are wellness-grade estimates from a finger/ear PPG sensor, not
a medical device; the dashboards leave them blank when there is no good contact.

### Relays and mains power
Test the climate controller with LEDs on the relay outputs first. Mains loads
(HVAC, dehumidifier) must go through an opto-isolated, fused relay board in an
enclosure, wired by a qualified person. De-energised = OFF is the safe state.
