"""
Simulator configuration — node definitions and MQTT settings.

TO INTEGRATE REAL SENSORS:
  - Set SIMULATED=false in your .env
  - Replace each node's MQTT publish calls with real sensor reads
  - Keep the same topic structure: imm/habitat/{node_id}/telemetry/{measurement}
  - Keep the same JSON payload schema
  - The backend, InfluxDB, and OpenMCT dashboards will work with zero changes.
"""

import os

# ── MQTT broker (from environment) ───────────────────────────────
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
PUBLISH_INTERVAL_S = float(os.getenv("PUBLISH_INTERVAL_S", "5"))

# ── Node definitions ─────────────────────────────────────────────
# When real hardware arrives:
#   - 'id'  maps to the physical node hostname or MAC-derived ID
#   - 'type' maps to the hardware type
#   - Sensor baseline values (temp_base, etc.) are just for simulation
#     and are not used when real sensors are connected

NODES = [
    {
        "id": "node-rpi-01",
        "type": "rpi",
        "location": "Habitat Zone A",
        "hardware": "Raspberry Pi 4 (4GB)",
        # Simulation parameters only — not used with real sensors
        "temp_base": 22.5,
        "humidity_base": 48.0,
        "pressure_base": 1013.25,
        "co2_base": 450,
    },
    {
        "id": "node-rpi-02",
        "type": "rpi",
        "location": "Habitat Zone B",
        "hardware": "Raspberry Pi 4 (4GB)",
        "temp_base": 23.1,
        "humidity_base": 52.0,
        "pressure_base": 1012.80,
        "co2_base": 480,
    },
    {
        "id": "node-jetson",
        "type": "jetson",
        "location": "Edge AI Node",
        "hardware": "NVIDIA Jetson Orin Nano (8GB)",
        # Simulation parameters
        "cpu_temp_base": 45.0,
        "gpu_temp_base": 52.0,
        "power_base": 12.0,
    },
]

# ── MQTT Topic Schema (reference) ────────────────────────────────
# imm/habitat/{node_id}/telemetry/{measurement}  — sensor readings
# imm/habitat/{node_id}/status                   — node heartbeat
# imm/mcc/command/{subsystem}                    — MCC → Edge commands
# imm/mcc/ack/{command_id}                       — Edge → MCC ack
