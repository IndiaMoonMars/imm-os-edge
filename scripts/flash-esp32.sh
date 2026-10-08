#!/usr/bin/env bash
# Build firmware/esp32-sensors on the Pi and flash the ESP32 board on a Pi USB port,
# then show the board's first output. Run as the normal user (no sudo):
#
#   cd ~/imm-os-edge && ./scripts/flash-esp32.sh            # internal board (firmware/esp32-sensors)
#   cd ~/imm-os-edge && ./scripts/flash-esp32.sh external   # external board (firmware/esp32-external)
#
# First run installs PlatformIO into ~/.imm-platformio and downloads the ESP32 toolchain
# (a few minutes); later runs only rebuild and flash. ESP32_PORT overrides the port.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOARD="${1:-internal}"
case "$BOARD" in
    internal) FW="$REPO/firmware/esp32-sensors"; SERVICE='imm-sensor-pipeline@esp32_bridge.py' ;;
    external) FW="$REPO/firmware/esp32-external"; SERVICE='imm-sensor-pipeline@external_board_bridge.py' ;;
    *) echo "✗ board is 'internal' or 'external'" >&2; exit 1 ;;
esac
PIO_HOME_VENV="${IMM_PIO_VENV:-$HOME/.imm-platformio}"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }

[ "$EUID" -ne 0 ] || die "run without sudo (PlatformIO installs into your home directory)"

step "ESP32 on USB"
# Which board is on which USB port is pinned in edge.env by setup-node.sh (from what each board prints).
# Use it, so the internal firmware can never be flashed onto the external board, or the other way round.
ENV_FILE="${IMM_ENV_FILE:-/etc/imm-os/edge.env}"
PINS=$( (cat "$ENV_FILE" 2>/dev/null || sudo -n cat "$ENV_FILE" 2>/dev/null || true) \
        | grep -E '^(ESP32_PORT|EXT_BOARD_PORT|EXT_BOARD_USB)=' || true)
pin() { echo "$PINS" | sed -n "s/^$1=//p" | tail -1 | tr -d "\"'"; }
INT_PIN=$(pin ESP32_PORT)
EXT_PIN=$(pin EXT_BOARD_PORT); [ -n "$EXT_PIN" ] || EXT_PIN=$(pin EXT_BOARD_USB)
case "$BOARD" in
    internal) PORT="${ESP32_PORT:-$INT_PIN}"; OTHER="$EXT_PIN" ;;
    external) PORT="${ESP32_PORT:-$EXT_PIN}"; OTHER="$INT_PIN" ;;
esac
if [ -z "$PORT" ]; then
    n=$( (ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true) | wc -l)
    [ "$n" -le 1 ] || die "$n USB serial boards are plugged in and none is pinned as the $BOARD board, so which one to flash isn't known. Run setup-node.sh first (--int-board both --ext-board both pins both), or name the port: ESP32_PORT=/dev/serial/by-id/… $0 $BOARD"
    PORT=$(cd "$REPO/core" && "$PY" -c 'from hw import esp32_port; print(esp32_port())')
fi
[ -n "$PORT" ] || die "no ESP32 found: plug the board into a Pi USB port with a data cable (check with: ls /dev/ttyUSB* /dev/ttyACM*)"
real() { local r; r=$(readlink -f "$1" 2>/dev/null || true); echo "${r:-$1}"; }   # an unplugged board: its name
if [ -n "$OTHER" ] && [ "$(real "$PORT")" = "$(real "$OTHER")" ]; then
    die "$PORT is pinned as the other board's port in $ENV_FILE: not flashing the $BOARD firmware onto it"
fi
echo "  ✓ $BOARD board on $PORT"
[ -r "$PORT" ] && [ -w "$PORT" ] || die "no access to $PORT: your user needs the dialout group (setup-node.sh adds it; log out and in, or reboot)"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    echo "  · stopping $SERVICE while flashing (it may hold the port)"
    sudo systemctl stop "$SERVICE"
    RESTART_BRIDGE=1
fi

step "PlatformIO"
if [ ! -x "$PIO_HOME_VENV/bin/pio" ]; then
    echo "  · installing PlatformIO into $PIO_HOME_VENV (one time)"
    python3 -m venv "$PIO_HOME_VENV"
    "$PIO_HOME_VENV/bin/pip" install -q --upgrade pip platformio
fi
echo "  ✓ $("$PIO_HOME_VENV/bin/pio" --version)"

step "Build and flash (first build downloads the ESP32 toolchain)"
"$PIO_HOME_VENV/bin/pio" run -d "$FW" -t upload --upload-port "$PORT"

step "Board output (15 s)"
sleep 1                                  # let the post-flash reset settle
"$PY" - "$PORT" <<'EOF'
import sys, time
import serial
ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = sys.argv[1], 115200, 1
ser.dtr = ser.rts = False              # don't hold the board in reset
ser.open()
end = time.time() + 15
while time.time() < end:
    line = ser.readline().decode("utf-8", "replace").rstrip()
    if line:
        print("  " + line, flush=True)
EOF

if [ "${RESTART_BRIDGE:-0}" = 1 ]; then sudo systemctl start "$SERVICE"; fi
echo
if [ "$BOARD" = external ]; then
    echo "Next: set the board's Wi-Fi (stored on the board, never in a file):"
    echo "  EXT_BOARD_PORT=$PORT .venv/bin/python sensor_drivers/external_board_bridge.py --send 'WIFI_SSID <network name>'"
    echo "  EXT_BOARD_PORT=$PORT .venv/bin/python sensor_drivers/external_board_bridge.py --send 'WIFI_PASS <password>'"
    echo "  then STATUS shows its IP: put EXT_BOARD_URL=http://<ip>/json in /etc/imm-os/edge.env"
else
    echo "Next: sudo .venv/bin/python tools/bringup.py esp32"
fi
