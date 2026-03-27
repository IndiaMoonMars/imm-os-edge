FROM python:3.10-slim

LABEL org.opencontainers.image.title="imm-sensor-simulator"
LABEL org.opencontainers.image.description="IMM-OS Sensor Simulator — replaces real RPi/Jetson nodes during development"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MQTT_HOST=mosquitto
ENV MQTT_PORT=1883
ENV PUBLISH_INTERVAL_S=5

CMD ["python", "-u", "sensor_sim.py"]
