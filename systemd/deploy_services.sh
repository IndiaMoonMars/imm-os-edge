#!/usr/bin/env bash
# IMM-OS edge node service deployment.
#
# Installs the systemd units (filling in where the code lives, which user runs it and
# which Python to use) and starts the services configured for this node:
#   sensor pipelines   IMM_SENSORS="bme280_driver.py scd40_driver.py"   (or pass as arguments)
#   ECLSS daemons      IMM_ECLSS_DAEMONS="water_monitor eclss_pid"
#   EVA daemons        IMM_EVA_DAEMONS="gps_driver uwb_driver position_fusion"
#   lighting listener  always (logs only until LIGHT_ZONES is set)
#   MCC discovery      always: a timer re-finds the MCC if its IP address changes
# Every service has a systemd watchdog (core/watchdog.py) and restarts forever; the Pi's
# hardware watchdog is switched on (systemd/imm-watchdog.conf).
# The lists are read from /etc/imm-os/edge.env; scripts/setup-node.sh writes them.
#
# Overridable: IMM_HOME (default: this checkout), IMM_USER (default: owner of IMM_HOME),
#              IMM_PYTHON (default: $IMM_HOME/.venv/bin/python if present, else /usr/bin/python3)
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./deploy_services.sh)"
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMM_HOME="${IMM_HOME:-$(dirname "$HERE")}"
IMM_USER="${IMM_USER:-$(stat -c %U "$IMM_HOME")}"
if [ -z "${IMM_PYTHON:-}" ]; then
    if [ -x "$IMM_HOME/.venv/bin/python" ]; then IMM_PYTHON="$IMM_HOME/.venv/bin/python"; else IMM_PYTHON=/usr/bin/python3; fi
fi

if [ -f /etc/imm-os/edge.env ]; then
    # shellcheck disable=SC1091
    . /etc/imm-os/edge.env
fi

install_unit() {
    # Units are written for /home/ubuntu/imm-os-edge + user ubuntu + system python3;
    # rewrite those for this node.
    sed -e "s#/home/ubuntu/imm-os-edge#$IMM_HOME#g" \
        -e "s#^User=ubuntu#User=$IMM_USER#" \
        -e "s#/usr/bin/python3#$IMM_PYTHON#g" \
        -e "s#sh -c 'python3 #sh -c '$IMM_PYTHON #" \
        -e "s#| python3 #| $IMM_PYTHON #g" \
        "$HERE/$1" > "/etc/systemd/system/$1"
    chmod 644 "/etc/systemd/system/$1"
}

echo "Installing units (code $IMM_HOME, user $IMM_USER, python $IMM_PYTHON)"
for unit in imm-sensor-pipeline@.service imm-lighting-controller.service imm-eclss@.service imm-eva@.service \
            imm-mcc-discovery.service imm-mcc-discovery.timer; do
    install_unit "$unit"
done
install -d -o "$IMM_USER" -g "$IMM_USER" /var/lib/imm-os /var/lib/imm-os/blackbox /var/lib/imm-os/spool
systemctl daemon-reload

# Hardware watchdog: a hung kernel or systemd resets the Pi (systemd/imm-watchdog.conf)
if [ -e /dev/watchdog ] || [ -e /dev/watchdog0 ]; then
    if ! cmp -s "$HERE/imm-watchdog.conf" /etc/systemd/system.conf.d/imm-watchdog.conf 2>/dev/null; then
        install -D -m 644 "$HERE/imm-watchdog.conf" /etc/systemd/system.conf.d/imm-watchdog.conf
        systemctl daemon-reexec
        echo "Hardware watchdog on (systemd pets it every few seconds; a hang resets the Pi)"
    fi
else
    echo "  ! no /dev/watchdog: hardware watchdog not enabled (add dtparam=watchdog=on to /boot/firmware/config.txt)"
fi

# Sensor pipelines: arguments win, then IMM_SENSORS. Nothing is started by default,
# so a node never runs drivers for hardware it doesn't have.
if [ "$#" -gt 0 ]; then
    sensors=("$@")
else
    read -r -a sensors <<< "${IMM_SENSORS:-}"
fi

# The list is the whole set: a driver taken out of it is stopped and disabled (otherwise it
# keeps running, e.g. esp32_bridge.py grabbing another board's USB port).
if [ "$#" -gt 0 ] || [ -n "${IMM_SENSORS+set}" ]; then
    wants=" ${sensors[*]:-} "
    for unit in $( { systemctl list-units --all --plain --no-legend 'imm-sensor-pipeline@*' 2>/dev/null | awk '{print $1}'
                     ls /etc/systemd/system/*.wants/ 2>/dev/null | grep '^imm-sensor-pipeline@' ; } | sort -u); do
        inst="${unit#imm-sensor-pipeline@}"; inst="${inst%.service}"
        case "$wants" in *" $inst "*) ;; *)
            echo "Stopping sensor pipeline $inst (no longer in IMM_SENSORS)"
            systemctl disable --now "$unit" >/dev/null 2>&1 || true ;;
        esac
    done
fi

for s in "${sensors[@]}"; do
    [ -f "$IMM_HOME/sensor_drivers/$s" ] || { echo "  ! unknown driver $s (not in sensor_drivers/)"; continue; }
    echo "Starting sensor pipeline $s"
    systemctl enable --now "imm-sensor-pipeline@$s"
done
for d in ${IMM_ECLSS_DAEMONS:-}; do
    [ -f "$IMM_HOME/eclss/$d.py" ] || { echo "  ! unknown ECLSS daemon $d"; continue; }
    echo "Starting ECLSS daemon $d"
    systemctl enable --now "imm-eclss@$d"
done
for d in ${IMM_EVA_DAEMONS:-}; do
    [ -f "$IMM_HOME/eva/$d.py" ] || { echo "  ! unknown EVA daemon $d"; continue; }
    echo "Starting EVA daemon $d"
    systemctl enable --now "imm-eva@$d"
done

echo "Starting MCC discovery (every minute: re-finds the MCC if its IP address changes)"
systemctl enable --now imm-mcc-discovery.timer

echo "Starting ECLSS lighting controller (MQTT listener)"
systemctl enable --now imm-lighting-controller.service

echo "Deployment complete. Status:"
sleep 2
systemctl --no-pager --lines=0 status 'imm-sensor-pipeline@*' 'imm-eclss@*' 'imm-eva@*' imm-lighting-controller.service imm-mcc-discovery.timer || true
