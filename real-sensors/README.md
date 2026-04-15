# 🛰️ Real Sensor Integration Guide

When your hardware arrives, connecting it requires **zero backend changes**.
The system is designed so real sensors publish to the exact same MQTT topics as the simulator.

---

## How the Data Flow Works

```
[Real Sensor / RPi / Jetson]
        │
        │  MQTT publish to:
        │  imm/habitat/{node_id}/telemetry/{measurement}
        ▼
[Mosquitto Broker :1883]
        │
        ▼
[Telemetry Worker]  ── writes to ──▶  [InfluxDB]
                                            │
                                            ▼
                                    [Backend API /api/telemetry]
                                            │
                                            ▼
                                    [Mission Dashboard / OpenMCT]
```

Real sensors → same topic schema → same backend → same dashboard. ✅

---

## Step-by-Step: Adding a Real Sensor

### Step 1 — Identify your node

Decide the `node_id` for your sensor. Use the existing IDs or add new ones:

| Node ID        | Hardware        | Measurements                                   |
|----------------|-----------------|------------------------------------------------|
| `node-rpi-01`  | Raspberry Pi 4  | temperature, humidity, pressure, co2, o2       |
| `node-rpi-02`  | Raspberry Pi 4  | temperature, humidity, pressure, co2, o2       |
| `node-jetson`  | Jetson Orin     | cpu_temp, gpu_temp, power_draw, battery_level  |
| `node-rpi-03`  | (future RPi)    | Add new ID here                                |

### Step 2 — Copy the driver template

```bash
cp templates/rpi-sensor-driver.py  /home/ubuntu/imm-driver.py      # for RPi
# OR
cp templates/jetson-sensor-driver.py /home/ubuntu/imm-driver.py    # for Jetson
```

### Step 3 — Edit the driver

Open `imm-driver.py` and update:
1. `NODE_ID` — your node's ID (from the table above)
2. `MQTT_HOST` — IP of the machine running Mosquitto (or `imm.local`)
3. Sensor reading functions — replace `_read_*()` stubs with real sensor library calls

### Step 4 — Disable the simulator for this node

In `imm-os-infra/docker-compose.yml`, edit the `sensor-sim` service environment:

```yaml
sensor-sim:
  environment:
    # Disable specific nodes as real hardware comes online
    DISABLED_NODES: "node-rpi-01"   # comma-separated
```

Or stop the simulator entirely when all nodes are real:
```bash
docker compose stop sensor-sim
```

### Step 5 — Install and run the driver on the hardware

```bash
pip install paho-mqtt
python imm-driver.py
```

Or use the systemd service template:
```bash
cp templates/imm-driver.service /etc/systemd/system/
systemctl enable --now imm-driver
```

### Step 6 — Verify data is flowing

```bash
# Subscribe to MQTT topics and watch for data:
mosquitto_sub -h imm.local -p 1883 -t "imm/habitat/#" -v
```

You should see JSON payloads every 5 seconds with `"simulated": false`.

---

## MQTT Message Schema

All messages (simulated AND real) must use this exact format:

```json
{
  "node_id":    "node-rpi-01",
  "node_type":  "rpi",
  "measurement": "temperature",
  "value":      22.5,
  "unit":       "celsius",
  "timestamp":  1712345678,
  "simulated":  false
}
```

**Topic format:** `imm/habitat/{node_id}/telemetry/{measurement}`

**Heartbeat topic:** `imm/habitat/{node_id}/status`
```json
{ "node_id": "node-rpi-01", "status": "online", "timestamp": 1712345678 }
```

---

## Supported Units

| Measurement     | Unit      |
|-----------------|-----------|
| temperature     | celsius   |
| humidity        | percent   |
| pressure        | hPa       |
| co2             | ppm       |
| o2              | percent   |
| cpu_temp        | celsius   |
| gpu_temp        | celsius   |
| power_draw      | watts     |
| battery_level   | percent   |
| solar_input     | watts     |

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| No data in dashboard | `mosquitto_sub -h imm.local -t "imm/habitat/#" -v` — is the driver publishing? |
| Data not in InfluxDB | `docker compose logs telemetry-worker` — is the bridge running? |
| Wrong values | Check sensor library calibration; simulator defaults are in `simulator/config.py` |
| Node offline | Check heartbeat topic: `imm/habitat/{node_id}/status` |
