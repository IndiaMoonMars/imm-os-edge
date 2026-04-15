"""
IMM-OS RPi Sensor Driver Template
==================================
Copy this file to your Raspberry Pi, fill in the sensor reading functions,
and run: python imm-driver.py

This publishes to the SAME MQTT topics as the simulator — zero backend changes.

Supported sensors (add/remove as needed):
  - Temperature/Humidity: DHT22, SHT31, BME280
  - Pressure:             BME280, BMP388
  - CO2:                  MHZ19, SCD30, SCD41
  - O2:                   KE-25F (analog via ADC), ME2-O2

Install dependencies:
  pip install paho-mqtt RPi.GPIO smbus2 bme280 mh-z19
"""

import json
import logging
import os
import time

import paho.mqtt.client as mqtt

# ════════════════════════════════════════════════════════════════════
# CONFIGURATION — Edit these for your node
# ════════════════════════════════════════════════════════════════════

NODE_ID   = "node-rpi-01"   # Change to "node-rpi-02" for second RPi
NODE_TYPE = "rpi"
NODE_ZONE = "Habitat Zone A"

MQTT_HOST = os.getenv("MQTT_HOST", "192.168.1.100")  # or "imm.local"
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
PUBLISH_INTERVAL_S = int(os.getenv("PUBLISH_INTERVAL_S", "5"))

# ════════════════════════════════════════════════════════════════════
# SENSOR READING FUNCTIONS — Replace stubs with real sensor code
# ════════════════════════════════════════════════════════════════════

# Example with BME280 (temperature + humidity + pressure):
# import smbus2, bme280
# port = 1; address = 0x76
# bus = smbus2.SMBus(port)
# calibration_params = bme280.load_calibration_params(bus, address)

def read_temperature() -> float:
    """Return temperature in Celsius.
    
    Example (BME280):
        data = bme280.sample(bus, address, calibration_params)
        return data.temperature
    """
    # TODO: Replace with real sensor read
    raise NotImplementedError("Connect your temperature sensor and implement this")


def read_humidity() -> float:
    """Return relative humidity in percent (0–100).
    
    Example (BME280):
        data = bme280.sample(bus, address, calibration_params)
        return data.humidity
    """
    raise NotImplementedError("Connect your humidity sensor and implement this")


def read_pressure() -> float:
    """Return atmospheric pressure in hPa.
    
    Example (BME280):
        data = bme280.sample(bus, address, calibration_params)
        return data.pressure
    """
    raise NotImplementedError("Connect your pressure sensor and implement this")


def read_co2() -> float:
    """Return CO2 concentration in ppm.
    
    Example (MH-Z19):
        import mh_z19
        return mh_z19.read()["CO2"]
    """
    raise NotImplementedError("Connect your CO2 sensor and implement this")


def read_o2() -> float:
    """Return O2 concentration in percent (≈20.9% normal).
    
    Example (KE-25F via ADC):
        voltage = read_adc_voltage(channel=0)
        return voltage / 3.3 * 25.0  # calibrate per sensor datasheet
    """
    raise NotImplementedError("Connect your O2 sensor and implement this")


# ════════════════════════════════════════════════════════════════════
# MQTT PUBLISHER — Do not edit below unless extending topic schema
# ════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("imm-rpi-driver")

SENSORS = [
    ("temperature", read_temperature, "celsius"),
    ("humidity",    read_humidity,    "percent"),
    ("pressure",    read_pressure,    "hPa"),
    ("co2",         read_co2,         "ppm"),
    ("o2",          read_o2,          "percent"),
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
            "simulated":   False,   # ← real sensor flag
        })
        result = client.publish(topic, payload, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("Published %s = %s %s", measurement, value, unit)
        else:
            log.warning("Publish failed on %s", topic)

    # Heartbeat
    client.publish(
        f"imm/habitat/{NODE_ID}/status",
        json.dumps({"node_id": NODE_ID, "status": "online", "timestamp": ts}),
        qos=0,
        retain=True,
    )


def main():
    client = mqtt.Client(client_id=f"imm-rpi-{NODE_ID}")
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    time.sleep(2)

    log.info("IMM-OS RPi driver started — node: %s (%s)", NODE_ID, NODE_ZONE)
    while True:
        publish_readings(client)
        time.sleep(PUBLISH_INTERVAL_S)


if __name__ == "__main__":
    main()
