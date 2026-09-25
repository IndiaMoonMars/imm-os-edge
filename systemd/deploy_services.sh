#!/usr/bin/env bash
# IMM-OS Edge Node Service Deployment Script
# Automatically enables and starts all physical sensor drivers.

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./deploy_services.sh)"
  exit 1
fi

echo "Deploying IMM-OS sensor pipelines..."

# Copy templates to systemd config folder
cp imm-sensor-pipeline@.service /etc/systemd/system/
cp imm-lighting-controller.service /etc/systemd/system/

systemctl daemon-reload

# Default: every habitat-node driver. To deploy only the sensors fitted to this
# node, pass them as arguments:  sudo ./deploy_services.sh bme280_driver.py scd40_driver.py
# (Jetson nodes: sudo ./deploy_services.sh jetson_driver.py)
if [ "$#" -gt 0 ]; then
    sensors=("$@")
else
    sensors=(
        "bme280_driver.py"
        "scd40_driver.py"
        "o2_driver.py"
        "mq7_uart_bridge.py"
        "biosensor_driver.py"
        "ecg_driver.py"
        "lux_driver.py"
        "power_driver.py"
    )
fi

# Enable and start each pipeline instance
for s in "${sensors[@]}"
do
    echo "Starting pipeline for $s..."
    systemctl enable "imm-sensor-pipeline@$s"
    systemctl start "imm-sensor-pipeline@$s"
done

echo "Starting ECLSS lighting controller (MQTT listener)..."
systemctl enable imm-lighting-controller.service
systemctl start imm-lighting-controller.service

echo "Deployment complete! Checking status:"
sleep 2
systemctl --no-pager status imm-sensor-pipeline@* imm-lighting-controller.service
