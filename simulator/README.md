# Sensor Simulator — IMM-OS Edge

Software-only replacement for the physical RPi and Jetson edge nodes.
Publishes realistic telemetry to MQTT so the entire backend/InfluxDB/OpenMCT
pipeline can be developed and tested **before hardware arrives**.

## Architecture

```
[sensor_sim.py]
   ↓ MQTT publish
[Mosquitto broker]
   ↓ subscribe
[telemetry_worker.py (backend)]
   ↓ write
[InfluxDB]
   ↓ query
[OpenMCT dashboard]
```

## Simulated Nodes

| Node ID | Type | Location | Sensors |
|---|---|---|---|
| `node-rpi-01` | Raspberry Pi 4 | Habitat Zone A | temp, humidity, pressure, CO2, O2 |
| `node-rpi-02` | Raspberry Pi 4 | Habitat Zone B | temp, humidity, pressure, CO2, O2 |
| `node-jetson` | Jetson Orin Nano | Edge AI | CPU temp, GPU temp, power, battery, solar |

## MQTT Topic Format

```
imm/habitat/{node_id}/telemetry/{measurement}
imm/habitat/{node_id}/status
```

**Payload schema:**
```json
{
  "node_id": "node-rpi-01",
  "node_type": "rpi",
  "measurement": "temperature",
  "value": 22.4,
  "unit": "celsius",
  "timestamp": 1711234567,
  "simulated": true
}
```

## Running Locally

```bash
pip install -r requirements.txt
MQTT_HOST=localhost python sensor_sim.py
```

## Running via Docker (from `imm-os-infra/`)

```bash
docker compose up sensor-sim
```

## Integrating Real Sensors

When your RPi/Jetson hardware arrives:

1. Deploy real sensor drivers to the physical node
2. Have them publish JSON to the **same MQTT topics** with the **same payload schema**
3. Set `"simulated": false` in the payload
4. Stop the simulator container

**No backend, InfluxDB, or dashboard changes required.**

See `config.py` for the full node and topic reference.
