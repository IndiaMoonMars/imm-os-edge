"""MQTT client for EVA drivers (same broker settings as every edge script)."""
import os

import paho.mqtt.client as mqtt


def connect(client_id: str) -> mqtt.Client:
    host, port = os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883"))
    client = mqtt.Client(client_id=client_id)
    # Broker requires auth (allow_anonymous false); user/topics in imm-os-infra mosquitto/config/acl
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS_CA"):  # broker TLS listener (8883); verifies cert + hostname
        client.tls_set(ca_certs=os.getenv("MQTT_TLS_CA"))
    client.reconnect_delay_set(1, 30)
    client.connect_async(host, port, 60)
    client.loop_start()
    return client


def crew_id() -> str:
    """Crew member wearing this suit kit: their IMM-OS (Keycloak) username, e.g. ev1."""
    return os.getenv("CREW_ID", "ev1").lower()
