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
