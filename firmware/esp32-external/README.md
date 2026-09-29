# External sensor board: GNSS + Geiger (ESP32)

| Part | Connection |
|---|---|
| ESP32 DevKit (30-pin) | 5 V from the buck converter into VIN |
| DFRobot TEL0157 GNSS (I2C, address 0x20) | SDA → GPIO21, SCL → GPIO22 |
| DFRobot SEN0463 Geiger (M4011 tube) | pulse → GPIO4 |
| Buck converter | 12 V adapter → 5 V |
| 220 µF / 25 V electrolytic | across the 5 V rail, at the ESP32 |

Once a second the firmware prints one JSON line on USB serial and serves the same line at
`http://<board-ip>/json`, with a small live page at `http://<board-ip>/`:

```json
{"ms":61000,"geiger":{"cpm":24.0,"usv_h":0.156,"counts":24,"window_s":60,"warming":0},
 "gnss":{"fix":1,"sats":9,"lat":19.07601,"lon":72.87765,"alt_m":14.2,"sog_kn":0.03,"cog_deg":0.0,"utc":"2026-09-29T08:30:01Z"},
 "board":{"uptime_s":61,"reset_reason":1,"boot_count":3,"i2c_err":0,"rssi_dbm":-58}}
```

On the Pi, `sensor_drivers/external_board_bridge.py` reads it, or the data of the board's own firmware (below), and publishes `geiger`, `gnss` and `board` to
`habitat/sensors/<sensor>/exterior`.

- **Geiger:** counted on an interrupt, with a 150 µs dead time so one tube pulse counts once. CPM is taken over a
  60 s window of 1 s buckets, and µSv/h = CPM ÷ 153.8 (M4011, Cs-137 equivalent). For the first minute `warming`
  is 1 and the MCC marks the dose rate *suspect*. Background is about 15–45 CPM (0.1–0.3 µSv/h). A flat 0 means
  a dead tube or a loose pulse wire, and the MCC flags it as stuck.
- **GNSS:** GPS, BeiDou and GLONASS. The antenna needs open sky, and a cold start takes a few minutes. The MCC
  alarms on dose rate: caution at 0.5 µSv/h, warning at 2.5, emergency at 25.
- **Faults:**
  - A 10 s hardware watchdog.
  - The reset reason and boot count go out in `board`.
  - The GNSS is re-probed every 30 s if it goes missing.
  - A stuck I2C bus is freed by clocking SCL.
  - Wi-Fi reconnects by itself, and USB output continues without it.

Wiring as built (tested): both modules on the 5 V rail from VIN, the 220 µF across the 5 V rail. Keep the
GNSS antenna under open sky and the Geiger tube's HV section (≈400 V) enclosed.

## Connect it to the Pi 5 and IMM-OS

**Option A: keep the firmware already on the board** (its own Wi-Fi dashboard). Nothing to flash:
the Pi reads the same data your dashboard shows.

1. The board and the Pi on the same Wi-Fi/LAN. Give the board a DHCP reservation in the router.
2. On the Pi, find what it serves:
   ```bash
   cd ~/imm-os-edge
   .venv/bin/python sensor_drivers/external_board_bridge.py --probe http://<board-ip>/   # or --find
   ```
   It prints the values it recognises and the URL to use (`✓ use EXT_BOARD_URL=…`). If the dashboard page
   loads its data by script, the probe finds that data URL in the page. If a value is missing (an unusual
   name), map it: `EXT_BOARD_MAP="cpm=geiger.clicks,lat=gps.y"` in `/etc/imm-os/edge.env`.
3. Add it to the node. From the MCC PC:
   ```powershell
   .\scripts\provision-pi.ps1 -PiUser pratham -PiHost node-rpi-01.local -Sensors "bme280_driver.py" -ExtBoard http://<board-ip>/
   ```
   Or on the Pi: `sudo scripts/setup-node.sh --ext-board http://<board-ip>/` (or `--ext-board find`).
   Both check the board, write `EXT_BOARD_URL`, and start `external_board_bridge.py` as a service.
4. Check: `sudo .venv/bin/python tools/bringup.py external`. Then in IMM-OS, the Sensors page shows
   **Radiation** and **Position** cards for zone *exterior*. The Health page shows the radiation measurement.

What the MCC does with it:
- Dose-rate alarms: caution at 0.5, warning at 2.5, emergency at 25 µSv/h.
- Board or Wi-Fi lost: the streams go *stale* after 15 s, and a *Sensor offline* caution is raised after
  120 s. The driver reconnects by itself.
- A flat zero count (dead tube) is flagged *stuck*.

Tested end to end on the MCC stack, with a stand-in board serving a dashboard page and a `/data` JSON:
- the probe found `/data`;
- readings were stored in InfluxDB and appeared on the realtime feed;
- 2.9 µSv/h raised the warning, and it returned to normal after;
- unplugging the board raised the offline cautions, and the streams recovered on their own when it was back.

**Option B: flash the IMM-OS firmware in this folder.** It adds a watchdog, I2C bus recovery, GNSS UTC, the
reset reason and a warm-up flag. It replaces the dashboard firmware, but has its own page at `http://<board-ip>/`.

### Flash (from the Pi)

```bash
cd ~/imm-os-edge && ./scripts/flash-esp32.sh external
```

**This replaces the dashboard code already on the ESP32.** The new firmware has its own page at
`http://<board-ip>/`. It needs the board on a Pi USB port. When the internal board is also on USB, set
`EXT_BOARD_PORT=/dev/serial/by-id/...` first so the right one is flashed.

### Wi-Fi (stored on the board, never in code or files)

Over USB, one command at a time:

```bash
export EXT_BOARD_PORT=/dev/serial/by-id/usb-...        # this board
.venv/bin/python sensor_drivers/external_board_bridge.py --send 'WIFI_SSID My Network'
.venv/bin/python sensor_drivers/external_board_bridge.py --send 'WIFI_PASS the-password'
.venv/bin/python sensor_drivers/external_board_bridge.py --send 'STATUS'     # prints the IP
```

The name and password are saved in the ESP32's flash (NVS) and survive power-off. `WIFI_OFF` forgets them.
Give the board a fixed IP (a DHCP reservation in the router). Then, in `/etc/imm-os/edge.env`:

```
EXT_BOARD_URL=http://192.168.1.77/json
```

Or leave the board on USB and set `EXT_BOARD_PORT` instead. Then start the driver:
`sudo systemctl enable --now imm-sensor-pipeline@external_board_bridge.py`.

## Tests (no hardware)

`tests/test_esp32_external.py` compiles this firmware with the Arduino stubs in `test/stub/`. It runs it
against a simulated TEL0157 register map, Geiger pulses, Wi-Fi and HTTP. It covers:
- fix decoding (southern and western hemispheres, negative altitude), no fix, and a GNSS missing at boot;
- CPM and dose rate, and ringing pulses counted once;
- the Wi-Fi commands (including names with spaces) and both endpoints;
- the watchdog, and recovering a stuck bus.
