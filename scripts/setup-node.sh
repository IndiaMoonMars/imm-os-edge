#!/usr/bin/env bash
# IMM-OS edge node setup — turns a fresh Raspberry Pi OS (Pi 4 or Pi 5, 64-bit) install
# into a working IMM-OS node. Safe to re-run: every step checks before it changes anything.
#
#   git clone https://github.com/IndiaMoonMars/imm-os-edge.git && cd imm-os-edge
#   scp <mcc>:imm-os-infra/mosquitto/certs/ca.crt /tmp/ca.crt
#   sudo ./scripts/setup-node.sh --node-id node-rpi-01 --zone zone_a \
#        --mcc-ip 192.168.1.107 --ca /tmp/ca.crt \
#        --sensors "bme280_driver.py scd40_driver.py o2_driver.py" \
#        --eclss "eclss_pid" --eva ""
#
# Secrets are read from --secrets-file, the environment or prompted for (never passed as
# arguments, so they don't end up in shell history or `ps`):
#   IMM_EDGE_CLIENT_SECRET   (IMM_EDGE_CLIENT_SECRET in imm-os-infra/.env)
#   MQTT_PASSWORD            (MQTT_EDGE_PASSWORD in imm-os-infra/.env)
#
# Steps: packages → interfaces (I2C, SPI, UART, 1-Wire) → user groups → Python venv →
#        /etc/imm-os (edge.env, CA, calibration.yaml) → /etc/hosts → local broker →
#        services → checks
#
# Store-and-forward: the node runs its own MQTT broker (localhost:1883). Every service
# publishes there; a bridge forwards to the MCC over TLS and queues on disk while the
# MCC or the network is down (tools/local_broker.py). --direct skips it (services then
# connect straight to the MCC and an outage loses what RAM can't hold).
#
# Options:
#   --node-id ID         this node's ID, shown on the dashboards (required)
#   --zone ZONE          habitat zone for its readings, e.g. zone_a (required)
#   --mcc-ip IP          LAN address of the MCC server (required unless --mcc-name resolves)
#   --mcc-name NAME      name in the MCC's TLS certificate (default imm.local)
#   --ca FILE            the MCC's MQTT CA certificate (required on first setup)
#   --secrets-file FILE  read the secrets below from FILE (KEY=value lines), then delete it
#   --sensors "…"        sensor_drivers/ to run, e.g. "bme280_driver.py scd40_driver.py"
#                        (sysmon_driver.py, the node health report, is always added)
#   --no-sysmon          don't add sysmon_driver.py
#   --eclss "…"          eclss/ daemons, e.g. "water_monitor eclss_pid"
#   --eva "…"            eva/ daemons, e.g. "gps_driver uwb_driver position_fusion"
#   --crew-id ID         wearer of this EVA kit (EVA nodes)
#   --int-board WHERE    the internal ESP32 sensor board, and adds esp32_bridge.py: usb (default),
#                        or http://<board-ip>/json once it is on Wi-Fi (WIFI_SSID / WIFI_PASS).
#                        With --ext-board usb too, both boards' ports are found and pinned in edge.env
#                        (USB keeps recording with the Wi-Fi off; the SD card gets every reading)
#   --ext-board WHERE    the external GNSS + Geiger board, and adds external_board_bridge.py:
#                        usb (on a Pi USB port; found by what it prints), /dev/serial/by-id/…,
#                        http://<board-ip>/ (over Wi-Fi), or find (search the local network)
#   --user USER          service user (default: the user who ran sudo)
#   --direct             no local broker: services connect straight to the MCC
#   --skip-apt           don't install packages
#   --skip-interfaces    don't touch I2C/SPI/UART/1-Wire settings
#   --no-services        configure only; don't start anything
#   --check-only         only run the health checks
#   --dry-run            print what would be done, change nothing
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_DIR="${IMM_CONF_DIR:-/etc/imm-os}"
ENV_FILE="$CONF_DIR/edge.env"
HOSTS_FILE="${IMM_HOSTS_FILE:-/etc/hosts}"

NODE_ID="" ZONE="" MCC_IP="" MCC_NAME="imm.local" CA="" CREW_ID="" SECRETS_FILE="" EXT_BOARD="" INT_BOARD=""
SENSORS="__unset__" ECLSS="__unset__" EVA="__unset__"
SVC_USER="${SUDO_USER:-}"
SKIP_APT=0 SKIP_IF=0 NO_SERVICES=0 CHECK_ONLY=0 DRY=0 NO_SYSMON=0 DIRECT=0
REBOOT_NEEDED=0

die()  { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*"; }
run()  { if [ "$DRY" = 1 ]; then echo "  + $*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --node-id) NODE_ID="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --mcc-ip) MCC_IP="$2"; shift 2 ;;
        --mcc-name) MCC_NAME="$2"; shift 2 ;;
        --ca) CA="$2"; shift 2 ;;
        --secrets-file) SECRETS_FILE="$2"; shift 2 ;;
        --sensors) SENSORS="$2"; shift 2 ;;
        --eclss) ECLSS="$2"; shift 2 ;;
        --eva) EVA="$2"; shift 2 ;;
        --crew-id) CREW_ID="$2"; shift 2 ;;
        --ext-board) EXT_BOARD="$2"; shift 2 ;;
        --int-board) INT_BOARD="$2"; shift 2 ;;
        --user) SVC_USER="$2"; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --direct) DIRECT=1; shift ;;
        --no-sysmon) NO_SYSMON=1; shift ;;
        --skip-interfaces) SKIP_IF=1; shift ;;
        --no-services) NO_SERVICES=1; shift ;;
        --check-only) CHECK_ONLY=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

[ "$DRY" = 1 ] || [ "$EUID" -eq 0 ] || die "run with sudo"
envget() { python3 "$REPO/tools/envfile.py" --get "$ENV_FILE" "$1" 2>/dev/null || true; }
MODEL=$(tr -d '\0' 2>/dev/null < "${IMM_MODEL_FILE:-/proc/device-tree/model}" || true)
IS_PI5=0; case "$MODEL" in *"Raspberry Pi 5"*) IS_PI5=1 ;; esac
BOOT_CONFIG="${IMM_BOOT_CONFIG:-/boot/firmware/config.txt}"; [ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt

# ── Health checks ─────────────────────────────────────────────────
FAILS=0
check() {   # check "label" command...
    local label="$1"; shift
    if out=$("$@" 2>&1); then echo "  ✓ $label${out:+ — $out}"; else echo "  ✗ $label${out:+ — $out}"; FAILS=$((FAILS + 1)); fi
}
ntp_synced() { [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; }
chk_time()   {   # right after boot the first NTP sync can take a little while
    local i; for i in $(seq "${IMM_NTP_WAIT_S:-60}"); do ntp_synced && return 0; sleep 1; done
    ntp_synced || { echo "clock not NTP-synced (readings >7 days off are rejected)"; return 1; }
}
chk_name()   { getent hosts "$1" | awk '{print $1}' | head -1 | grep . || { echo "$1 does not resolve"; return 1; }; }
chk_port()   { timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null && echo "port $2 open" || { echo "cannot reach $1:$2"; return 1; }; }
mcc_get() { local v; v=$(envget "MCC_$1"); [ -n "$v" ] || v=$(envget "$2"); echo "$v"; }
chk_mqtt() {   # logs in to the MCC broker directly (TLS), whatever the node's services use
    local host port user pass ca out
    host=$(mcc_get HOST MQTT_HOST); port=$(mcc_get MQTT_PORT MQTT_PORT); user=$(mcc_get MQTT_USERNAME MQTT_USERNAME)
    pass=$(mcc_get MQTT_PASSWORD MQTT_PASSWORD); ca=$(mcc_get TLS_CA MQTT_TLS_CA)
    # read-only probe: retained lighting commands (exit 27 = timed out waiting, but connected)
    out=$(mosquitto_sub -h "$host" -p "$port" --cafile "$ca" -u "$user" -P "$pass" \
          -t 'habitat/control/lighting/#' -C 1 -W 5 2>&1) && { echo "TLS + login OK"; return 0; }
    case "$out" in
        *"not authorised"*|*"Not authorized"*) echo "login refused: MCC_MQTT_PASSWORD wrong?"; return 1 ;;
        *certificate*|*"host name"*|*TLS*|*SSL*|*"Protocol error"*)
            echo "TLS handshake failed: is $ca the MCC's ca.crt, and MCC_HOST ($host) a name in its certificate?"; return 1 ;;
        *"Lookup error"*) echo "cannot resolve $host (hosts entry / --mcc-ip)"; return 1 ;;
        *[Tt]imed*|"") echo "TLS + login OK (no retained lighting yet)"; return 0 ;;
        *) echo "${out:0:120}"; return 1 ;;
    esac
}
chk_bridge() {   # the local broker's link to the MCC
    systemctl is-active --quiet mosquitto || { echo "mosquitto (local broker) not running"; return 1; }
    "$REPO/.venv/bin/python" "$REPO/tools/local_broker.py" status
}
chk_token() {
    local url secret body
    url=$(envget KEYCLOAK_TOKEN_URL); secret=$(envget IMM_EDGE_CLIENT_SECRET)
    body=$(curl -s -m 8 -d grant_type=client_credentials -d client_id="$(envget IMM_EDGE_CLIENT_ID)" \
           --data-urlencode client_secret="$secret" "$url") || { echo "Keycloak unreachable at $url"; return 1; }
    case "$body" in *access_token*) echo "edge token issued" ;; *) echo "no token: ${body:0:100}"; return 1 ;; esac
}
chk_power() {
    command -v vcgencmd >/dev/null || { echo "vcgencmd not available (not a Raspberry Pi?)"; return 0; }
    local t; t=$(vcgencmd get_throttled 2>/dev/null | sed -n 's/^throttled=//p')
    [ -n "$t" ] || { echo "cannot read throttle state (service user needs the video group)"; return 0; }
    if (( t & 0x1 )); then echo "UNDER-VOLTAGE now ($t): use the official supply (Pi 5: 27 W, 5 V/5 A)"; return 1; fi
    if (( t & 0x10000 )); then echo "under-voltage occurred since boot ($t): check supply and cable"; return 1; fi
    echo "supply OK ($t)"
}
chk_i2c() {
    command -v i2cdetect >/dev/null && [ -e /dev/i2c-1 ] || { echo "I2C bus 1 not available (reboot after setup?)"; return 1; }
    local found names=""
    found=$(i2cdetect -y 1 2>/dev/null | awk 'NR>1{for(i=2;i<=NF;i++) if($i ~ /^[0-9a-f][0-9a-f]$/ || $i=="UU") print $i}')
    for a in $found; do
        case "$a" in
            76|77) names+=" BME280@0x$a" ;; 62) names+=" SCD40@0x62" ;; 48|49) names+=" ADS1115@0x$a" ;;
            40|41) names+=" INA219/PCA9685@0x$a" ;; 57) names+=" MAX30100@0x57" ;; 70) names+=" TCA9548A@0x70" ;;
            63) names+=" EZO-pH@0x63" ;; 5a) names+=" MLX90614@0x5a" ;; 39) names+=" TSL2561@0x39" ;; 36) names+=" BMS@0x36" ;;
            *) names+=" ?@0x$a" ;;
        esac
    done
    echo "${names:- no I2C devices found}"
}

run_checks() {
    step "Health checks"
    local host; host=$(mcc_get HOST MQTT_HOST); host=${host:-$MCC_NAME}
    echo "  · board: ${MODEL:-unknown}"
    check "power supply" chk_power
    check "clock synchronised" chk_time
    check "$host resolves" chk_name "$host"
    check "MQTT TLS port" chk_port "$host" "$(mcc_get MQTT_PORT MQTT_PORT)"
    check "MQTT login" chk_mqtt
    if [ "$(envget IMM_LOCAL_BROKER)" = true ]; then check "store-and-forward link" chk_bridge; fi
    check "Keycloak edge login" chk_token
    check "I2C devices" chk_i2c
    if command -v systemctl >/dev/null; then
        local failed
        failed=$(systemctl list-units --no-legend --state=failed 'imm-*' 2>/dev/null | awk '{print $1}' | tr '\n' ' ' || true)
        if [ -n "$failed" ]; then echo "  ✗ failed services: $failed"; FAILS=$((FAILS + 1)); else ok "no failed imm-* services"; fi
    fi
    echo
    if [ "$FAILS" -eq 0 ]; then echo "All checks passed."; else echo "$FAILS check(s) failed — see above."; fi
    echo "Bench-test each sensor with: sudo $REPO/.venv/bin/python $REPO/tools/bringup.py <sensor>   (--list)"
    return "$FAILS"
}

if [ "$CHECK_ONLY" = 1 ]; then
    run_checks
    exit $?
fi

# ── Inputs ────────────────────────────────────────────────────────
[ -n "$NODE_ID" ] || NODE_ID=$(envget IMM_NODE_ID)
[ -n "$ZONE" ] || ZONE=$(envget IMM_ZONE)
[ -n "$NODE_ID" ] || die "--node-id is required (e.g. node-rpi-01)"
[ -n "$ZONE" ] || die "--zone is required (e.g. zone_a)"
[[ "$NODE_ID" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "--node-id: letters, digits, - and _ only"
[[ "$ZONE" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "--zone: letters, digits, - and _ only"
[ -n "$SVC_USER" ] || die "--user is required when not run via sudo"
id "$SVC_USER" >/dev/null 2>&1 || die "user $SVC_USER does not exist"
if [ ! -f "$CONF_DIR/mqtt-ca.crt" ] && [ -z "$CA" ]; then die "--ca is required on first setup (copy imm-os-infra/mosquitto/certs/ca.crt from the MCC)"; fi
if [ -n "$CA" ]; then
    [ -f "$CA" ] || die "--ca $CA not found"
    openssl x509 -in "$CA" -noout 2>/dev/null || die "--ca $CA is not a PEM certificate"
fi
if [ -n "$MCC_IP" ]; then
    [[ "$MCC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--mcc-ip must be an IPv4 address"
fi

secret_input() {   # secret_input VAR "prompt"
    local var="$1" current
    current=$(envget "$var")
    [ -n "$current" ] || [ "$var" != MQTT_PASSWORD ] || current=$(envget MCC_MQTT_PASSWORD)
    if [ -n "${!var:-}" ]; then return; fi
    if [ -n "$current" ]; then printf -v "$var" '%s' "$current"; return; fi
    [ "$DRY" = 1 ] && { printf -v "$var" '%s' "<secret>"; return; }
    [ -t 0 ] || die "$var not set: export it (sudo -E) or run interactively"
    read -r -s -p "  $2: " "${var?}"; echo
    [ -n "${!var}" ] || die "$var is required"
}
NAME_RE=$(printf '%s' "$MCC_NAME" | sed 's/[.]/\\./g')
step "Secrets"
if [ -n "$SECRETS_FILE" ]; then   # plain KEY=value lines; never sourced, so values can't run code
    [ -f "$SECRETS_FILE" ] || die "--secrets-file $SECRETS_FILE not found"
    for v in IMM_EDGE_CLIENT_SECRET MQTT_PASSWORD; do
        val=$(sed -n "s/^$v=//p" "$SECRETS_FILE" | tr -d '\r' | head -1)
        [ -n "$val" ] || die "$v missing from $SECRETS_FILE"
        printf -v "$v" '%s' "$val"
    done
    [ "$DRY" = 1 ] || rm -f "$SECRETS_FILE"
    ok "secrets read from $SECRETS_FILE"
fi
secret_input IMM_EDGE_CLIENT_SECRET "IMM_EDGE_CLIENT_SECRET (from imm-os-infra/.env)"
secret_input MQTT_PASSWORD "MQTT_PASSWORD (MQTT_EDGE_PASSWORD from imm-os-infra/.env)"
ok "secrets provided (not shown)"

# ── 1. Packages ───────────────────────────────────────────────────
if [ "$SKIP_APT" = 0 ]; then
    step "Packages"
    run apt-get update -qq
    # C-extension Python libraries come from the OS, so pip never needs a compiler
    os_py=()
    for p in python3-lgpio python3-spidev python3-evdev; do
        if apt-cache show "$p" >/dev/null 2>&1; then os_py+=("$p"); else warn "$p not in the package lists; the drivers that need it won't start"; fi
    done
    # avahi-daemon + libnss-mdns let the Pi resolve .local names, so a board reached as
    # http://imm-sensors.local/json is found whatever IP the router gave it (no reserved IP).
    run apt-get install -y -qq --no-install-recommends \
        python3-venv python3-dev python3-pip git curl openssl i2c-tools mosquitto-clients \
        avahi-daemon libnss-mdns "${os_py[@]}"
    if [ "$DIRECT" = 0 ]; then run apt-get install -y -qq --no-install-recommends mosquitto; fi
    ok "system packages installed"
fi

# ── 2. Interfaces ─────────────────────────────────────────────────
if [ "$SKIP_IF" = 0 ]; then
    step "Interfaces"
    if command -v raspi-config >/dev/null; then
        before=$(md5sum "$BOOT_CONFIG" 2>/dev/null || true)
        run raspi-config nonint do_i2c 0
        run raspi-config nonint do_spi 0
        run raspi-config nonint do_serial_hw 0      # UART on (GPS, MQ-7 STM32)
        run raspi-config nonint do_serial_cons 1    # login console off the UART
        run raspi-config nonint do_onewire 0        # DS18B20 on GPIO4
        # Pi 5: the header UART (GPIO14/15) is UART0 → /dev/ttyAMA0; make sure it is on
        # (/dev/serial0 is the separate debug connector there). core/hw.py picks ttyAMA0.
        if [ "$IS_PI5" = 1 ] && ! grep -qE '^dtparam=uart0(=on)?$' "$BOOT_CONFIG"; then
            if [ "$DRY" = 1 ]; then echo "  + append dtparam=uart0=on to $BOOT_CONFIG"; else printf '\n[all]\ndtparam=uart0=on\n' >> "$BOOT_CONFIG"; fi
        fi
        after=$(md5sum "$BOOT_CONFIG" 2>/dev/null || true)
        [ "$before" = "$after" ] || REBOOT_NEEDED=1
        ok "I2C, SPI, UART and 1-Wire enabled${MODEL:+ on $MODEL}"
    else
        warn "raspi-config not found (not Raspberry Pi OS?): enable I2C/SPI/UART with the board's own tool"
    fi
fi

# ── 3. User groups ────────────────────────────────────────────────
step "Service user $SVC_USER"
for g in gpio i2c spi dialout input video; do   # video: vcgencmd (power/throttle state)
    if getent group "$g" >/dev/null; then
        if id -nG "$SVC_USER" | tr ' ' '\n' | grep -qx "$g"; then :; else run usermod -aG "$g" "$SVC_USER"; REBOOT_NEEDED=1; fi
    fi
done
groups_now=$(id -nG "$SVC_USER" | tr ' ' '\n' | grep -xE 'gpio|i2c|spi|dialout|input|video' | tr '\n' ' ' || true)
ok "hardware groups: ${groups_now:-none yet (added on this run; active after reboot)}"

# ── 4. Python environment ─────────────────────────────────────────
step "Python environment ($REPO/.venv)"
VENV="$REPO/.venv"
OWNER=$(stat -c %U "$REPO")   # build the venv as whoever owns the checkout
if [ ! -x "$VENV/bin/python" ]; then
    # system site packages: lgpio, spidev and evdev come from the OS packages above
    run sudo -u "$OWNER" python3 -m venv --system-site-packages "$VENV"
fi
run sudo -u "$OWNER" "$VENV/bin/pip" install -q --upgrade pip
run sudo -u "$OWNER" "$VENV/bin/pip" install -q -r "$REPO/requirements.txt"
ok "requirements installed"

# ── 5. Configuration ──────────────────────────────────────────────
step "Configuration ($CONF_DIR)"
run install -d -m 755 "$CONF_DIR"
if [ ! -f "$ENV_FILE" ]; then
    run install -m 600 "$REPO/systemd/edge.env.example" "$ENV_FILE"
    ok "created $ENV_FILE from the example"
fi
updates=(
    "IMM_NODE_ID=$NODE_ID" "IMM_ZONE=$ZONE"
    "MCC_HOST=$MCC_NAME" "MCC_MQTT_PORT=8883" "MCC_MQTT_USERNAME=imm-edge" "MCC_TLS_CA=$CONF_DIR/mqtt-ca.crt"
    "KEYCLOAK_TOKEN_URL=http://$MCC_NAME/auth/realms/IndiaMoonMars/protocol/openid-connect/token"
    "IMM_EDGE_CLIENT_ID=imm-edge"
    "ECLSS_API_URL=http://$MCC_NAME/eclss" "EVA_API_URL=http://$MCC_NAME/eva"
    "INVENTORY_API_URL=http://$MCC_NAME/inventory"
    "IMM_CALIBRATION_FILE=$CONF_DIR/calibration.yaml"
)
if [ "$NO_SYSMON" = 0 ]; then
    [ "$SENSORS" != "__unset__" ] || SENSORS=$(envget IMM_SENSORS)
    case " $SENSORS " in *" sysmon_driver.py "*) ;; *) SENSORS=$(echo "sysmon_driver.py $SENSORS" | xargs) ;; esac
fi
if [ -n "$INT_BOARD" ]; then
    case "$INT_BOARD" in
        usb) updates+=("ESP32_URL=") ; ok "internal sensor board: USB" ;;
        http://*|https://*)
            case "$INT_BOARD" in */json) ;; *) INT_BOARD="${INT_BOARD%/}/json" ;; esac
            if curl -fsS -m 5 "$INT_BOARD" 2>/dev/null | grep -q '"ms"'; then ok "internal sensor board answers at $INT_BOARD"
            else warn "the internal sensor board did not answer at $INT_BOARD now (is it on Wi-Fi? STATUS over USB shows its IP)"; fi
            updates+=("ESP32_URL=$INT_BOARD") ;;
        *) die "--int-board: usb or http://<board-ip>/json" ;;
    esac
    [ "$SENSORS" != "__unset__" ] || SENSORS=$(envget IMM_SENSORS)
    case " $SENSORS " in *" esp32_bridge.py "*) ;; *) SENSORS=$(echo "$SENSORS esp32_bridge.py" | xargs) ;; esac
fi
if [ -n "$EXT_BOARD" ]; then
    PYBIN="$REPO/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN=python3
    BRIDGE="${IMM_EXT_BOARD_BRIDGE:-$REPO/sensor_drivers/external_board_bridge.py}"   # (tests: a stand-in)
    case "$EXT_BOARD" in
        usb|/dev/*)      # on a Pi USB port: find which port, and its speed, from what it prints
            # The board readers hold their USB ports, so nothing could be heard on them: stop them
            # while listening, and start them again straight after, whatever was heard (a failed
            # setup must never leave the node recording nothing).
            readers=""
            if [ "$DRY" = 0 ] && command -v systemctl >/dev/null; then
                readers=$(systemctl list-units --no-legend --plain --state=active \
                          'imm-sensor-pipeline@esp32_bridge.py.service' 'imm-sensor-pipeline@external_board_bridge.py.service' \
                          2>/dev/null | awk '{print $1}' | xargs || true)
                [ -z "$readers" ] || { echo "  · pausing the board readers while listening: $readers"; systemctl stop $readers; }
            fi
            echo "  · listening to the USB serial ports for the external board …"
            ARG=""; [ "$EXT_BOARD" = usb ] || ARG="$EXT_BOARD"
            heard=$("$PYBIN" "$BRIDGE" --listen $ARG --reset 2>&1 | tee /dev/stderr || true)
            if [ -n "$readers" ]; then systemctl start $readers; echo "  · board readers started again"; fi
            found=$(echo "$heard" | sed -n 's/.*EXT_BOARD_PORT=\([^ ]*\) EXT_BOARD_BAUD=\([0-9]*\).*/\1 \2/p' | head -1)
            EXT_BOARD=$(echo "$heard" | sed -n 's/.*EXT_BOARD_URL=\(http[^ ]*\).*/\1/p' | head -1)
            # Both boards on USB: pin the internal one's port too, so neither reader can take the other's
            internal=$(echo "$heard" | sed -n 's/.*internal sensor board: ESP32_PORT=\([^ ]*\).*/\1/p' | head -1)
            if [ "$INT_BOARD" = usb ] && [ -n "$internal" ]; then
                updates+=("ESP32_PORT=$internal")
                ok "internal sensor board on $internal"
            elif [ "$INT_BOARD" = usb ]; then
                warn "the internal sensor board was not heard on USB (data cable? flashed?); its reader takes the first other USB board"
            fi
            if [ -n "$found" ]; then
                updates+=("EXT_BOARD_PORT=${found% *}" "EXT_BOARD_BAUD=${found#* }" "EXT_BOARD_URL=")
                ok "external board on ${found% *} at ${found#* } baud"
            elif [ -n "$EXT_BOARD" ]; then
                ok "external board: its sketch prints no readings on USB, but serves them at $EXT_BOARD"
            else
                die "external board not recognised on USB (see its output above). If it says 'no USB serial device', the Pi sees no board on USB at all: check with lsusb (runbook 5.6). Nothing was changed; the readers carry on as before."
            fi ;;
        find)
            echo "  · looking for the external board on the local network …"
            EXT_BOARD=$("$PYBIN" "$BRIDGE" --find 2>/dev/null \
                        | sed -n 's/.*EXT_BOARD_URL=\(http[^ ]*\).*/\1/p' | head -1 || true)
            [ -n "$EXT_BOARD" ] || die "external board not found: is it on the same Wi-Fi as the Pi? Pass --ext-board http://<board-ip>/"
            ok "external board: $EXT_BOARD" ;;
        *)
            EXT_BOARD=$("$PYBIN" "$BRIDGE" --probe "$EXT_BOARD" 2>/dev/null \
                        | sed -n 's/.*EXT_BOARD_URL=\(http[^ ]*\).*/\1/p' | head -1 || true)
            [ -n "$EXT_BOARD" ] || warn "the external board did not answer with GNSS/Geiger values now; set EXT_BOARD_URL later" ;;
    esac
    [ -z "$EXT_BOARD" ] || updates+=("EXT_BOARD_URL=$EXT_BOARD" "EXT_BOARD_PORT=")
    [ "$SENSORS" != "__unset__" ] || SENSORS=$(envget IMM_SENSORS)
    case " $SENSORS " in *" external_board_bridge.py "*) ;; *) SENSORS=$(echo "$SENSORS external_board_bridge.py" | xargs) ;; esac
fi
[ "$SENSORS" = "__unset__" ] || updates+=("IMM_SENSORS=$SENSORS")
[ "$ECLSS" = "__unset__" ] || updates+=("IMM_ECLSS_DAEMONS=$ECLSS")
[ "$EVA" = "__unset__" ] || updates+=("IMM_EVA_DAEMONS=$EVA")
[ -z "$CREW_ID" ] || updates+=("CREW_ID=$(echo "$CREW_ID" | tr '[:upper:]' '[:lower:]')")
# where the node's own services publish: the local broker, or the MCC directly (--direct)
if [ "$DIRECT" = 0 ]; then
    updates+=("IMM_LOCAL_BROKER=true" "MQTT_HOST=localhost" "MQTT_PORT=1883" "MQTT_USERNAME=" "MQTT_TLS_CA=")
    mqtt_secret="MQTT_PASSWORD="
else
    updates+=("IMM_LOCAL_BROKER=false" "MQTT_HOST=$MCC_NAME" "MQTT_PORT=8883" "MQTT_USERNAME=imm-edge"
              "MQTT_TLS_CA=$CONF_DIR/mqtt-ca.crt")
    mqtt_secret="MQTT_PASSWORD=$MQTT_PASSWORD"
fi
if [ "$DRY" = 1 ]; then
    echo "  + set in $ENV_FILE: ${updates[*]} IMM_EDGE_CLIENT_SECRET=… MCC_MQTT_PASSWORD=…"
else
    python3 "$REPO/tools/envfile.py" "$ENV_FILE" "${updates[@]}" \
        "IMM_EDGE_CLIENT_SECRET=$IMM_EDGE_CLIENT_SECRET" "MCC_MQTT_PASSWORD=$MQTT_PASSWORD" "$mqtt_secret"
    chmod 600 "$ENV_FILE"
fi
ok "edge.env: node $NODE_ID, zone $ZONE, MCC $MCC_NAME:8883$([ "$DIRECT" = 0 ] && echo ", services via the local broker")"

if [ -n "$CA" ]; then
    run install -m 644 "$CA" "$CONF_DIR/mqtt-ca.crt"
    ok "CA certificate installed"
fi
if [ ! -f "$CONF_DIR/calibration.yaml" ]; then
    run install -m 644 "$REPO/systemd/calibration.yaml.example" "$CONF_DIR/calibration.yaml"
    ok "calibration.yaml created (no corrections yet)"
else
    run python3 "$REPO/tools/calibrate.py" --file "$CONF_DIR/calibration.yaml" check
fi

# ── 6. MCC name resolution ────────────────────────────────────────
if [ -n "$MCC_IP" ]; then
    step "Hosts file"
    set_host_entry() {   # set_host_entry FILE LABEL
        local file="$1" label="$2" current
        if grep -qE "^[0-9.]+[[:space:]]+$NAME_RE([[:space:]]|$)" "$file"; then
            current=$(awk -v n="$MCC_NAME" '$0 !~ /^#/ {for(i=2;i<=NF;i++) if($i==n) print $1}' "$file" | head -1)
            if [ "$current" != "$MCC_IP" ]; then
                run sed -i -E "s#^[0-9.]+([[:space:]]+)$NAME_RE([[:space:]]|\$)#$MCC_IP\1$MCC_NAME\2#" "$file"
                ok "$label: $MCC_NAME → $MCC_IP (was $current)"
            else
                ok "$label: $MCC_NAME → $MCC_IP already"
            fi
        else
            if [ "$DRY" = 1 ]; then echo "  + append '$MCC_IP $MCC_NAME' to $file"; else echo "$MCC_IP $MCC_NAME" >> "$file"; fi
            ok "$label: $MCC_NAME → $MCC_IP added"
        fi
    }
    set_host_entry "$HOSTS_FILE" "$HOSTS_FILE"
    # Raspberry Pi OS set up by Imager 2 uses cloud-init, which rewrites /etc/hosts from
    # a template at every boot; without the entry there too, it's gone after a reboot.
    for tmpl in "${IMM_CLOUD_HOSTS_TEMPLATES:-/etc/cloud/templates}"/hosts.*.tmpl; do
        if [ -f "$tmpl" ]; then set_host_entry "$tmpl" "cloud-init template $(basename "$tmpl")"; fi
    done
fi

# ── 7. Local broker (store-and-forward) ───────────────────────────
if [ "$DIRECT" = 0 ]; then
    step "Local broker"
    if [ "$DRY" = 1 ]; then
        echo "  + tools/local_broker.py write (bridge to $MCC_NAME:8883), enable + restart mosquitto"
    else
        rc=0
        python3 "$REPO/tools/local_broker.py" write --env-file "$ENV_FILE" || rc=$?
        [ "$rc" = 0 ] || [ "$rc" = 3 ] || die "could not write the local broker config"
        systemctl enable --quiet mosquitto
        if [ "$rc" = 3 ] || ! systemctl is-active --quiet mosquitto; then systemctl restart mosquitto; fi
        ok "mosquitto on localhost:1883, bridged to $MCC_NAME:8883 (queues on disk while the MCC is away)"
    fi
fi

# ── 8. Services ───────────────────────────────────────────────────
if [ "$NO_SERVICES" = 0 ]; then
    step "Services"
    run env IMM_HOME="$REPO" IMM_USER="$SVC_USER" IMM_PYTHON="$VENV/bin/python" "$REPO/systemd/deploy_services.sh"
fi

# ── 9. Checks ─────────────────────────────────────────────────────
if [ "$DRY" = 1 ]; then
    echo; echo "Dry run: nothing was changed."
    exit 0
fi
if [ "$REBOOT_NEEDED" = 1 ]; then
    echo
    echo "Interfaces or group membership changed: reboot, then run"
    echo "  sudo $REPO/scripts/setup-node.sh --check-only"
    exit 0
fi
run_checks
