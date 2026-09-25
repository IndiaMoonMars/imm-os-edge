#!/usr/bin/env python3
import time
import argparse
import logging
import random
import os
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [lighting] %(message)s")
log = logging.getLogger(__name__)

# HARDWARE MOCK (PCA9685 I2C)
class PCA9685_Mock:
    def set_pwm(self, channel, on, off):
        log.debug(f"I2C Cmd: CH{channel} ON={on} OFF={off}")

pwm = PCA9685_Mock()

def set_lighting(zone: str, brightness: int, kelvin: int):
    # WS2812B logical translation based on color temperature (Kelvin)
    # 2000K (Warm) -> 6500K (Daylight)
    brightness = max(0, min(100, brightness))
    kelvin = max(2000, min(6500, kelvin))
    
    # Send simulated I2C commands
    log.info(f"Targeting {zone} -> Brightness: {brightness}%, Color Temp: {kelvin}K")
    pwm.set_pwm(0, 0, int(40.95 * brightness))
    
def circadian_loop():
    log.info("Starting Auto-Circadian Loop (12 Hour compression)")
    # Simulation: compress 12 hours into 60 seconds
    schedule = [
        (2500, "06:00 - Dawn"),
        (3500, "08:00 - Morning"),
        (5500, "12:00 - Midday"),
        (4000, "16:00 - Afternoon"),
        (2000, "20:00 - Evening")
    ]
    for temp, phase in schedule:
        log.info(f"Circadian Phase: {phase}")
        set_lighting("all_zones", 80, temp)
        time.sleep(12)

# Commands published (retained, QoS 1) by the ECLSS API on PUT /api/v1/eclss/lighting/{zone}
LIGHTING_TOPIC = "habitat/control/lighting/+"


def on_lighting_message(client, userdata, msg):
    zone = msg.topic.rsplit("/", 1)[-1]
    try:
        cmd = json.loads(msg.payload)
        set_lighting(zone, int(cmd["brightness"]), int(cmd["kelvin"]))
    except (ValueError, KeyError, TypeError) as e:
        log.error(f"Ignoring malformed lighting command on {msg.topic}: {e}")


def listen_loop():
    import paho.mqtt.client as mqtt

    host = os.getenv("MQTT_HOST", "localhost")
    port = int(os.getenv("MQTT_PORT", "1883"))

    def on_connect(client, userdata, flags, rc):
        if rc != 0:
            log.error(f"MQTT connect failed rc={rc}")
            return
        # (Re)subscribe on every connect; retained messages replay current state
        client.subscribe(LIGHTING_TOPIC, qos=1)
        log.info(f"Listening for lighting commands on {LIGHTING_TOPIC} @ {host}:{port}")

    client = mqtt.Client(client_id=f"imm-lighting-{os.uname().nodename}", clean_session=True)
    # Broker requires auth (allow_anonymous false); user/topics in imm-os-infra mosquitto/config/acl
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):  # broker TLS listener (8883); verifies cert + hostname
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.on_connect = on_connect
    # TLS/DNS/refused errors are otherwise swallowed by the retry loop
    client.on_connect_fail = lambda c, u: log.error(
        f"MQTT connection to {host}:{port} failed (check MQTT_HOST matches the broker certificate, "
        f"MQTT_TLS_CA and credentials); retrying")
    client.on_message = on_lighting_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(host, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", type=str, default="core")
    parser.add_argument("--brightness", type=int, default=80)
    parser.add_argument("--kelvin", type=int, default=5000)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--listen", action="store_true",
                        help="Subscribe to ECLSS API lighting commands over MQTT")
    args = parser.parse_args()

    if args.listen:
        listen_loop()
    elif args.auto:
        circadian_loop()
    else:
        set_lighting(args.zone, args.brightness, args.kelvin)
