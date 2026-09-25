# 🛰️ Real Sensor Integration Guide

Real sensors and the simulator publish **the same readings on the same MQTT topics**,
so moving from simulated to real data needs no backend changes. Each reading
carries `"simulated": true/false` and the dashboard shows a **LIVE** or **SIM**
badge for every value.

---

## How the data flows

```
[sensor_drivers/*.py on RPi / Jetson]        [sensor-sim container]
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
| `jetson` | `cpu_temp`, `gpu_temp`, `power_w` | `jetson_driver.py` |
| `bms` | `battery_pct`, `solar_w` | simulated for now |

The schema lives in `imm-os-backend/services/telemetry_schema.py`.

---

## Bringing a sensor online

1. **Provision the node** (see the Hardware Integration Plan): OS, I2C/UART, and
   `/etc/imm-os/edge.env` with `IMM_NODE_ID`, `IMM_ZONE`, the MQTT password and the CA cert.
2. **Bench-test in stdout mode**, which prints readings and sends nothing:
   ```bash
   python3 sensor_drivers/bme280_driver.py
   ```
3. **Go live.** The systemd unit runs the driver with `--mode both` (MQTT plus the
   local encrypted blackbox):
   ```bash
   sudo ./systemd/deploy_services.sh bme280_driver.py     # only the sensors fitted here
   journalctl -u imm-sensor-pipeline@bme280_driver.py -f
   ```
4. **Stop simulating that sensor** on the MCC. In `imm-os-infra/.env`:
   ```
   SIM_DISABLED_SENSORS=node-rpi-01:bme280
   ```
   then `docker compose up -d sensor-sim`. A bare `bme280` stops it on every
   simulated node. When everything is real: `docker compose stop sensor-sim`.
5. **Check** the Overview page: the value shows **LIVE**. If it doesn't, look for
   rejected readings:
   ```bash
   docker compose logs telemetry-validator --tail 20
   ```

---

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

---

## ECLSS and EVA hardware (Phase 5)

Every script in `eclss/` and `eva/` drives real hardware by default. Add
`--simulate` (or `IMM_SIMULATE=true` in edge.env) to run the old synthetic
behaviour without hardware. Settings for all of them are in
`systemd/edge.env.example`.

**One-time node setup** (Raspberry Pi OS): enable I2C, SPI, the UART and 1-Wire,
then give the service user access to the hardware:
```bash
sudo raspi-config nonint do_i2c 0 && sudo raspi-config nonint do_spi 0
echo "dtoverlay=w1-gpio" | sudo tee -a /boot/firmware/config.txt     # DS18B20 on GPIO4
sudo usermod -aG gpio,i2c,spi,dialout,input ubuntu && sudo reboot
```

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

Run them as services by listing them in edge.env and re-running the deploy script:
```bash
IMM_ECLSS_DAEMONS="water_monitor waste_tracker eclss_pid"
IMM_EVA_DAEMONS="gps_driver uwb_driver position_fusion eva_biosensor_driver"
sudo ./systemd/deploy_services.sh
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
