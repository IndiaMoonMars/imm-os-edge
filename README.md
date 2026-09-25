# IMM OS Edge
Sensor, ECLSS and EVA code for the IMM-OS edge nodes (Raspberry Pi 4 / Pi 5, STM32 co-processors).

## Set up a node

On a fresh Raspberry Pi OS Lite (64-bit) install, with the MCC stack running:

```bash
git clone https://github.com/IndiaMoonMars/imm-os-edge.git && cd imm-os-edge
scp <you>@<mcc-ip>:<path>/imm-os-infra/mosquitto/certs/ca.crt /tmp/ca.crt
sudo ./scripts/setup-node.sh --node-id node-rpi-01 --zone zone_a --mcc-ip 192.168.1.107 \
     --ca /tmp/ca.crt --sensors "bme280_driver.py scd40_driver.py o2_driver.py" --eclss "eclss_pid"
```

It asks for `IMM_EDGE_CLIENT_SECRET` and `MQTT_PASSWORD` (from `imm-os-infra/.env`),
installs packages and a Python venv, enables I2C/SPI/UART/1-Wire, writes
`/etc/imm-os/edge.env`, the CA and `calibration.yaml`, points `imm.local` at the MCC,
starts the services and checks clock, MQTT (TLS + login), Keycloak and the I2C bus.
Safe to re-run; `--dry-run` shows what it would do, `--check-only` just runs the checks.

## Bring sensors online one at a time

```bash
sudo .venv/bin/python tools/bringup.py              # board, power supply, I2C/UART/SPI/1-Wire
sudo .venv/bin/python tools/bringup.py scd40        # bus check + real driver + value ranges
```

When a sensor passes, add its driver with `setup-node.sh --sensors "…"` and switch it off
in the MCC simulator (`SIM_DISABLED_SENSORS`). Every node also runs `sysmon_driver.py`
(node health: temperature, load, power supply, Pi 5 PMIC power and fan).

## Calibrate sensors

```bash
IMM_CALIBRATION_OFF=1 .venv/bin/python sensor_drivers/bme280_driver.py        # raw readings
sudo .venv/bin/python tools/calibrate.py one-point bme280.temp --raw 23.6 --true 22.8 --reference "Testo 605i"
sudo .venv/bin/python tools/calibrate.py report                               # sign-off sheet
```

Corrections live in `/etc/imm-os/calibration.yaml` and apply to every reading before it
leaves the node (no restart needed).

More: [real-sensors/README.md](real-sensors/README.md) (data flow, drivers, ECLSS/EVA hardware).
