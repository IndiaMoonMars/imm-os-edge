"""
IMM-OS Jetson Sensor Driver Template
=======================================
Copy this file to your Jetson Orin, fill in the sensor reading functions,
and run: python imm-driver.py

Publishes compute/power telemetry to the same MQTT schema as the simulator.

Jetson-specific reads use the tegrastats utility + jtop library:
  pip install jetson-stats paho-mqtt

Sensors typically available on Jetson Orin:
  - CPU temperature:  /sys/class/thermal/thermal_zone*/temp
  - GPU temperature:  tegrastats or jtop
  - Power draw:       /sys/bus/i2c/drivers/ina3221/ (on-board INA3221)
  - Battery level:    via external BMS over I2C if battery module fitted
  - Solar input:      via INA219 or similar external ADC module
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# ════════════════════════════════════════════════════════════════════
# CONFIGURATION — Edit for your Jetson node
# ════════════════════════════════════════════════════════════════════

NODE_ID   = "node-jetson"
NODE_TYPE = "jetson"
NODE_ZONE = "Compute / Power"

MQTT_HOST = os.getenv("MQTT_HOST", "192.168.1.100")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
PUBLISH_INTERVAL_S = int(os.getenv("PUBLISH_INTERVAL_S", "5"))

# ════════════════════════════════════════════════════════════════════
# SENSOR READING FUNCTIONS — Implement per your hardware config
# ════════════════════════════════════════════════════════════════════

def read_cpu_temp() -> float:
    """Return CPU package temperature in Celsius.
    
    Example (reading Jetson thermal zones):
        temps = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            type_file = zone / "type"
            temp_file = zone / "temp"
            if "CPU" in type_file.read_text():
                temps.append(int(temp_file.read_text()) / 1000.0)
        return sum(temps) / len(temps)
    
    Or with jtop:
        from jtop import jtop
        with jtop() as jetson:
            return jetson.temperature["CPU"]
    """
    raise NotImplementedError("Implement CPU temperature read for Jetson")


def read_gpu_temp() -> float:
    """Return GPU temperature in Celsius.
    
    Example with jtop:
        from jtop import jtop
        with jtop() as jetson:
            return jetson.temperature["GPU"]
    """
    raise NotImplementedError("Implement GPU temperature read for Jetson")


def read_power_draw() -> float:
    """Return total system power draw in Watts.
    
    Jetson Orin has an on-board INA3221 power monitor.
    Example (reading via sysfs):
        for rail_dir in Path("/sys/bus/i2c/drivers/ina3221").glob("*/iio:device*"):
            current_path = rail_dir / "in_current0_input"
            voltage_path = rail_dir / "in_voltage0_input"
            if current_path.exists() and voltage_path.exists():
                mA = float(current_path.read_text())
                mV = float(voltage_path.read_text())
                return round((mA * mV) / 1e6, 2)  # → Watts
    
    Or with jtop:
        from jtop import jtop
        with jtop() as jetson:
            return jetson.power["tot"]["power"] / 1000.0  # mW → W
    """
    raise NotImplementedError("Implement power draw read for Jetson")


def read_battery_level() -> float:
    """Return battery charge level in percent.
    
    If using an external battery module (e.g., Waveshare UPS HAT) over I2C:
        import smbus2
        bus = smbus2.SMBus(1)
        # Read SoC register per your BMS datasheet
        soc = bus.read_byte_data(0x36, 0x04)
        return soc * 100 / 255
    
    If no battery fitted, return a constant or remove this measurement.
    """
    raise NotImplementedError("Implement battery level read (or return 100.0 if no battery)")


def read_solar_input() -> float:
    """Return solar panel input power in Watts.
    
    Example (INA219 via smbus2):
        bus = smbus2.SMBus(1)
        # Read bus voltage + shunt voltage from INA219 at 0x40
        # Compute power = V * I
        return computed_watts
    
    If no solar panel yet, return 0.0.
    """
    raise NotImplementedError("Implement solar input read (or return 0.0 if no solar module)")


# ════════════════════════════════════════════════════════════════════
# MQTT PUBLISHER — Same as RPi template; do not change topic format
# ════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("imm-jetson-driver")

SENSORS = [
    ("cpu_temp",      read_cpu_temp,      "celsius"),
    ("gpu_temp",      read_gpu_temp,      "celsius"),
    ("power_draw",    read_power_draw,    "watts"),
    ("battery_level", read_battery_level, "percent"),
    ("solar_input",   read_solar_input,   "watts"),
]


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
    else:
        log.error("MQTT connect failed rc=%s", rc)


def publish_readings(client: mqtt.Client):
    ts = int(time.time())
    for measurement, reader_fn, unit in SENSORS:
        try:
            value = round(float(reader_fn()), 3)
        except NotImplementedError:
            log.warning("Sensor not implemented: %s — skipping", measurement)
            continue
        except Exception as exc:
            log.error("Error reading %s: %s", measurement, exc)
            continue

        topic = f"imm/habitat/{NODE_ID}/telemetry/{measurement}"
        payload = json.dumps({
            "node_id":     NODE_ID,
            "node_type":   NODE_TYPE,
            "measurement": measurement,
            "value":       value,
            "unit":        unit,
            "timestamp":   ts,
            "simulated":   False,
        })
        result = client.publish(topic, payload, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("Published %s = %s %s", measurement, value, unit)
        else:
            log.warning("Publish failed on %s", topic)

    client.publish(
        f"imm/habitat/{NODE_ID}/status",
        json.dumps({"node_id": NODE_ID, "status": "online", "timestamp": ts}),
        qos=0, retain=True,
    )


def main():
    client = mqtt.Client(client_id=f"imm-jetson-{NODE_ID}")
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    time.sleep(2)

    log.info("IMM-OS Jetson driver started — node: %s (%s)", NODE_ID, NODE_ZONE)
    while True:
        publish_readings(client)
        time.sleep(PUBLISH_INTERVAL_S)


if __name__ == "__main__":
    main()
