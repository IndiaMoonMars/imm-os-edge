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

# ECLSS / EVA daemons fitted to this node, e.g. in /etc/imm-os/edge.env:
#   IMM_ECLSS_DAEMONS="water_monitor shower_timer waste_tracker biolab_monitor eclss_pid"
#   IMM_EVA_DAEMONS="gps_driver uwb_driver position_fusion eva_biosensor_driver tool_tracker"
cp imm-eclss@.service imm-eva@.service /etc/systemd/system/
systemctl daemon-reload
if [ -f /etc/imm-os/edge.env ]; then
    # shellcheck disable=SC1091
    . /etc/imm-os/edge.env
fi
for d in ${IMM_ECLSS_DAEMONS:-}; do
    echo "Starting ECLSS daemon $d..."
    systemctl enable --now "imm-eclss@$d"
done
for d in ${IMM_EVA_DAEMONS:-}; do
    echo "Starting EVA daemon $d..."
    systemctl enable --now "imm-eva@$d"
done

echo "Starting ECLSS lighting controller (MQTT listener)..."
systemctl enable imm-lighting-controller.service
systemctl start imm-lighting-controller.service

echo "Deployment complete! Checking status:"
sleep 2
systemctl --no-pager status 'imm-sensor-pipeline@*' 'imm-eclss@*' 'imm-eva@*' imm-lighting-controller.service
