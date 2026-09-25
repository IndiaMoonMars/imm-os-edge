#!/usr/bin/env python3
import time
import argparse
import logging
import os
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [lighting] %(message)s")
log = logging.getLogger(__name__)

# ── Tunable-white LED output (PCA9685 PWM board) ────────────────────
# Each zone has two constant-current/MOSFET-driven LED channels: a warm-white strip
# (~2700 K) and a cool-white strip (~6500 K). Mixing their duty cycles sets the colour
# temperature; the sum sets brightness. Zone → PCA9685 channels comes from
#   LIGHT_ZONES="core:0,1;galley:2,3;sleep:4,5"     (warm channel, cool channel)
WARM_K, COOL_K = 2700, 6500
GAMMA = 2.2  # perceived brightness is roughly linear in duty^(1/2.2)


def mix(brightness: int, kelvin: int):
    """(warm, cool) duty cycles 0..1 for a brightness 0–100 % and colour temperature."""
    b = (max(0, min(100, brightness)) / 100.0) ** GAMMA
    cool = (max(WARM_K, min(COOL_K, kelvin)) - WARM_K) / (COOL_K - WARM_K)
    return round(b * (1 - cool), 4), round(b * cool, 4)


def parse_zones(spec: str) -> dict:
    zones = {}
    for part in filter(None, (x.strip() for x in (spec or "").split(";"))):
        name, chans = part.split(":")
        warm, cool = (int(c) for c in chans.split(","))
        zones[name.strip()] = (warm, cool)
    return zones


class PCA9685Output:
    def __init__(self, zones: dict):
        import board
        import busio
        from adafruit_pca9685 import PCA9685
        self.pca = PCA9685(busio.I2C(board.SCL, board.SDA), address=int(os.getenv("LIGHT_PCA_ADDRESS", "0x40"), 16))
        self.pca.frequency = int(os.getenv("LIGHT_PWM_HZ", "1000"))  # above visible flicker
        self.zones = zones

    def apply(self, zone: str, warm: float, cool: float) -> None:
        targets = self.zones.values() if zone == "all_zones" else [self.zones[zone]] if zone in self.zones else []
        if not targets:
            log.warning(f"Zone '{zone}' has no LED channels in LIGHT_ZONES; ignored")
        for w_ch, c_ch in targets:
            self.pca.channels[w_ch].duty_cycle = int(warm * 0xFFFF)
            self.pca.channels[c_ch].duty_cycle = int(cool * 0xFFFF)


class LogOutput:
    def apply(self, zone: str, warm: float, cool: float) -> None:
        log.debug(f"{zone}: warm {warm:.3f} cool {cool:.3f} (no LED hardware)")


def make_output():
    zones = parse_zones(os.getenv("LIGHT_ZONES", ""))
    if os.getenv("IMM_SIMULATE", "false").lower() == "true" or not zones:
        if not zones:
            log.warning("LIGHT_ZONES not set: logging lighting commands only")
        return LogOutput()
    return PCA9685Output(zones)


output = None


def set_lighting(zone: str, brightness: int, kelvin: int):
    global output
    if output is None:
        output = make_output()
    brightness = max(0, min(100, brightness))
    kelvin = max(2000, min(6500, kelvin))
    warm, cool = mix(brightness, kelvin)
    log.info(f"Targeting {zone} -> Brightness: {brightness}%, Color Temp: {kelvin}K (warm {warm:.2f}, cool {cool:.2f})")
    output.apply(zone, warm, cool)


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
