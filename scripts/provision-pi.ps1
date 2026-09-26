<#
.SYNOPSIS
    Set up a Raspberry Pi as an IMM-OS edge node from the MCC PC, over SSH.

.DESCRIPTION
    Run it in PowerShell on the Windows PC that runs the IMM-OS stack, after flashing
    Raspberry Pi OS Lite (64-bit) with SSH enabled and booting the Pi. It:

      1. finds the Pi, and this PC's LAN address as the Pi sees it;
      2. sets up SSH key login (asks for the Pi's password once);
      3. copies this imm-os-edge checkout (committed files) and the MQTT CA to the Pi;
      4. runs scripts/setup-node.sh there. The secrets come from imm-os-infra\.env and
         go over SSH into a file only the Pi user can read, which setup deletes; they
         never appear on a command line;
      5. reboots the Pi, then runs the health checks and tools/bringup.py all;
      6. if the checks pass, stops simulating this node's health data (sysmon), and the
         sensors in -Sensors if they also passed bring-up, so their cards show LIVE.

    Safe to re-run: setup-node.sh only changes what isn't set up yet.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\provision-pi.ps1 -PiUser pratham

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\provision-pi.ps1 -PiUser pratham -PiHost 192.168.1.42 -Sensors "bme280_driver.py scd40_driver.py"
#>
param(
    # Username set in Raspberry Pi Imager
    [Parameter(Mandatory = $true)][string]$PiUser,
    # Hostname set in Raspberry Pi Imager (with .local), or the Pi's IP address
    [string]$PiHost = 'node-rpi-01.local',
    [string]$NodeId = 'node-rpi-01',
    [string]$Zone = 'zone_a',
    # Sensor drivers to run, e.g. "bme280_driver.py scd40_driver.py". Leave empty until
    # each sensor has passed tools/bringup.py; node health (sysmon) always runs.
    [string]$Sensors = '',
    # This PC's LAN address; found automatically when empty
    [string]$MccIp = '',
    [string]$InfraDir = '',
    [int]$SshPort = 22,
    # Keep simulating this node's health data even when the checks pass
    [switch]$KeepSim,
    # Show what setup would do on the Pi; change nothing there, no reboot
    [switch]$DryRun
)

# Kept ASCII-only: Windows PowerShell 5.1 misreads other characters in files without a BOM.
# ssh/scp/git are called directly (not through a helper) so their output reaches the
# console as it happens; $LASTEXITCODE is read right after each call.
# Remote commands never contain double quotes: 5.1 mangles them in native arguments.

$ErrorActionPreference = 'Stop'
$EdgeDir = Split-Path -Parent $PSScriptRoot
if (-not $InfraDir) { $InfraDir = Join-Path (Split-Path -Parent $EdgeDir) 'imm-os-infra' }
$Target = "$PiUser@$PiHost"
$SshOpts = @('-p', "$SshPort", '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=15')
$ScpOpts = @('-P', "$SshPort", '-o', 'StrictHostKeyChecking=accept-new')

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  !   $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host ''; Write-Host "FAILED: $text" -ForegroundColor Red; exit 1 }

function Read-DotEnv([string]$path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $key = $Matches[1]; $value = $Matches[2]
            if ($value -match '^"(.*)"$') { $value = $Matches[1] }
            elseif ($value -match "^'(.*)'$") { $value = $Matches[1] }
            $values[$key] = $value
        }
    }
    return $values
}

function Get-LocalAddressFor([System.Net.IPAddress]$remote) {
    # The OS picks the interface that routes to $remote; a UDP connect sends nothing
    $sock = New-Object System.Net.Sockets.Socket(
        [System.Net.Sockets.AddressFamily]::InterNetwork,
        [System.Net.Sockets.SocketType]::Dgram, [System.Net.Sockets.ProtocolType]::Udp)
    try { $sock.Connect($remote, 9); return $sock.LocalEndPoint.Address.ToString() }
    finally { $sock.Close() }
}

# ---- 0. This PC ------------------------------------------------------------
Step 'Checking this PC'
foreach ($tool in 'ssh', 'scp', 'git') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool not found. ssh/scp: Settings > System > Optional features > OpenSSH Client. git: https://git-scm.com"
    }
}
$envFile = Join-Path $InfraDir '.env'
$caFile = Join-Path (Join-Path (Join-Path $InfraDir 'mosquitto') 'certs') 'ca.crt'
if (-not (Test-Path -LiteralPath $envFile)) { Fail "$envFile not found (use -InfraDir)" }
if (-not (Test-Path -LiteralPath $caFile)) { Fail "$caFile not found. Create it with imm-os-infra\mosquitto\gen-certs.ps1" }
$dotenv = Read-DotEnv $envFile
$edgeSecret = $dotenv['IMM_EDGE_CLIENT_SECRET']
$mqttPassword = $dotenv['MQTT_EDGE_PASSWORD']
if (-not $edgeSecret) { Fail "IMM_EDGE_CLIENT_SECRET is empty in $envFile" }
if (-not $mqttPassword) { Fail "MQTT_EDGE_PASSWORD is empty in $envFile" }
Ok "secrets and MQTT CA found in $InfraDir"
$commit = (& git -C $EdgeDir log -1 --format='%h %s' | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { Fail "$EdgeDir is not a git checkout" }
Ok "imm-os-edge at $commit"

# ---- 1. Find the Pi --------------------------------------------------------
Step "Finding $PiHost"
$piIp = $null
try {
    $piIp = [System.Net.Dns]::GetHostAddresses($PiHost) |
        Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
} catch { }
if (-not $piIp) {
    Fail "can't find $PiHost. Is the Pi powered on and on the same Wi-Fi/LAN as this PC? Wait 2-3 minutes after its first boot, or pass -PiHost <its IP from your router's device list>."
}
Ok "Pi at $piIp"
if (-not $MccIp) { $MccIp = Get-LocalAddressFor $piIp }
Ok "this PC (MCC) is $MccIp as seen from the Pi"

$onWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or $IsWindows
if ($onWindows) {
    # The Pi reaches MQTT on 8883 and Keycloak/APIs on 80 on this PC
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    $ruleName = 'IMM-OS edge nodes (MQTT 8883, web 80)'
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($rule) { Ok 'firewall rule present' }
    elseif ($isAdmin) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 80, 8883 `
            -Action Allow -Profile Private, Domain | Out-Null
        Ok 'firewall: allowed inbound TCP 80 and 8883 on private networks'
    } else {
        Warn 'not running as Administrator, so the firewall rule was not added. If the health checks say MQTT or Keycloak is unreachable, re-run this from PowerShell opened with "Run as administrator".'
    }
    try {
        $ifIndex = (Get-NetIPAddress -IPAddress $MccIp -ErrorAction Stop).InterfaceIndex
        $category = (Get-NetConnectionProfile -InterfaceIndex $ifIndex -ErrorAction Stop).NetworkCategory
        if ("$category" -eq 'Public') {
            Warn "this network is marked Public, so Windows blocks the Pi. As Administrator run: Set-NetConnectionProfile -InterfaceIndex $ifIndex -NetworkCategory Private"
        }
    } catch { }
}

# ---- 2. SSH key login ------------------------------------------------------
Step 'SSH login'
& ssh -q @SshOpts -o BatchMode=yes -o ConnectTimeout=10 $Target true
if ($LASTEXITCODE -eq 0) {
    Ok 'key login already works'
} else {
    $sshDir = Join-Path $HOME '.ssh'
    $key = Join-Path $sshDir 'id_ed25519'
    if (-not (Test-Path -LiteralPath "$key.pub")) {
        if (-not (Test-Path -LiteralPath $sshDir)) { New-Item -ItemType Directory -Path $sshDir | Out-Null }
        # cmd passes the empty passphrase through intact; Windows PowerShell 5.1 would drop ""
        if ($onWindows) { cmd /c "ssh-keygen -q -t ed25519 -N `"`" -f `"$key`"" }
        else { & ssh-keygen -q -t ed25519 -N '' -f $key }
        if ($LASTEXITCODE -ne 0) { Fail 'ssh-keygen failed' }
        Ok "created SSH key $key"
    }
    Write-Host "  Enter the Pi password for $PiUser (the one set in Raspberry Pi Imager):"
    Get-Content -LiteralPath "$key.pub" |
        & ssh @SshOpts $Target "umask 077; mkdir -p ~/.ssh; tr -d '\r' >> ~/.ssh/authorized_keys"
    if ($LASTEXITCODE -ne 0) {
        Fail "couldn't log in to $Target. Check the username and password set in Imager. If you re-flashed the Pi and ssh warns that the host identification changed, run: ssh-keygen -R $PiHost"
    }
    & ssh -q @SshOpts -o BatchMode=yes $Target true
    if ($LASTEXITCODE -ne 0) { Fail 'key login still fails after installing the key' }
    Ok 'key login set up (no more password prompts)'
}

# ---- 3. Code and CA --------------------------------------------------------
Step 'Copying imm-os-edge and the MQTT CA to the Pi'
$stage = Join-Path ([System.IO.Path]::GetTempPath()) 'imm-provision'
if (Test-Path -LiteralPath $stage) { Remove-Item -Recurse -Force -LiteralPath $stage }
New-Item -ItemType Directory -Path $stage | Out-Null
# autocrlf off: shell scripts must reach the Pi with LF line endings
& git -C $EdgeDir -c core.autocrlf=false archive --format=tar.gz -o (Join-Path $stage 'imm-os-edge.tar.gz') HEAD
if ($LASTEXITCODE -ne 0) { Fail 'git archive failed' }
Copy-Item -LiteralPath $caFile -Destination (Join-Path $stage 'mqtt-ca.crt')
Push-Location $stage
try {
    # relative names: scp would read C:\... as a host called C
    & scp -q @ScpOpts 'imm-os-edge.tar.gz' 'mqtt-ca.crt' "${Target}:"
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { Fail 'scp failed' }
& ssh @SshOpts $Target 'mkdir -p ~/imm-os-edge && tar xzf ~/imm-os-edge.tar.gz -C ~/imm-os-edge && rm ~/imm-os-edge.tar.gz'
if ($LASTEXITCODE -ne 0) { Fail 'unpacking on the Pi failed' }
Remove-Item -Recurse -Force -LiteralPath $stage
Ok 'code in ~/imm-os-edge, CA in ~/mqtt-ca.crt'

# ---- 4. Setup --------------------------------------------------------------
Step 'Running setup-node.sh on the Pi (first run takes 5-15 minutes)'
"IMM_EDGE_CLIENT_SECRET=$edgeSecret`nMQTT_PASSWORD=$mqttPassword`n" |
    & ssh @SshOpts $Target "umask 077; tr -d '\r' > ~/.imm-secrets"
if ($LASTEXITCODE -ne 0) { Fail 'copying the secrets failed' }
$setup = 'cd ~/imm-os-edge && sudo ./scripts/setup-node.sh --secrets-file ~/.imm-secrets' +
    " --node-id $NodeId --zone $Zone --mcc-ip $MccIp --ca ~/mqtt-ca.crt"
if ($Sensors) { $setup += " --sensors '$Sensors'" }
if ($DryRun) { $setup += ' --dry-run' }
& ssh -t @SshOpts $Target $setup
$rc = $LASTEXITCODE
& ssh @SshOpts $Target 'rm -f ~/.imm-secrets'
if ($rc -ne 0) { Fail "setup-node.sh exited with $rc. The output above shows which step failed." }
Ok 'setup finished'

if ($DryRun) { Write-Host ''; Write-Host 'Dry run: nothing was changed on the Pi.'; exit 0 }

# ---- 5. Reboot, checks, bring-up -------------------------------------------
Step 'Rebooting the Pi (turns on I2C, SPI and UART)'
& ssh -t @SshOpts $Target 'sudo systemctl reboot'
Start-Sleep -Seconds 20
$deadline = (Get-Date).AddMinutes(4)
$back = $false
while ((Get-Date) -lt $deadline) {
    & ssh -q @SshOpts -o BatchMode=yes -o ConnectTimeout=5 $Target true
    if ($LASTEXITCODE -eq 0) { $back = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $back) { Fail "the Pi didn't come back within 4 minutes. Power-cycle it and re-run this script." }
Ok 'Pi is back'

Step 'Health checks'
& ssh -t @SshOpts $Target 'cd ~/imm-os-edge && sudo ./scripts/setup-node.sh --check-only'
$checks = $LASTEXITCODE

Step 'Sensor bring-up (tools/bringup.py all)'
& ssh -t @SshOpts $Target 'cd ~/imm-os-edge && sudo .venv/bin/python tools/bringup.py all'
$bringup = $LASTEXITCODE

# ---- 6. Real data LIVE on the dashboard ------------------------------------
Step 'Dashboard'
if ($checks -ne 0) {
    Warn 'health checks failed, so the simulator was left as it is. Fix the checks marked with a cross above and re-run this script.'
    exit 1
}
# Simulator streams each driver takes over; node health (sysmon) always runs
$simByDriver = @{
    'esp32_bridge.py' = @('bme280', 'scd40', 'o2'); 'bme280_driver.py' = @('bme280'); 'scd40_driver.py' = @('scd40')
    'o2_driver.py' = @('o2'); 'mq7_uart_bridge.py' = @('mq7'); 'lux_driver.py' = @('tsl2561'); 'bms_driver.py' = @('bms')
}
$simNames = @('sysmon')
$drivers = @($Sensors -split '\s+' | Where-Object { $_ })
if ($drivers.Count -gt 0) {
    if ($bringup -eq 0) {
        foreach ($d in $drivers) { if ($simByDriver.ContainsKey($d)) { $simNames += $simByDriver[$d] } }
    } else {
        Warn 'a sensor failed bring-up (see the summary above), so only node health switches from SIM to LIVE. Fix it and re-run this script.'
    }
}
if ($KeepSim) {
    Ok 'checks passed; -KeepSim: simulator unchanged'
} else {
    $text = [System.IO.File]::ReadAllText($envFile)
    $entries = @($simNames | Select-Object -Unique | ForEach-Object { "${NodeId}:$_" })
    $m = [regex]::Match($text, '(?m)^SIM_DISABLED_SENSORS=([^\r\n]*)')
    if ($m.Success) {
        $list = @($m.Groups[1].Value.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        foreach ($e in $entries) { if ($list -notcontains $e) { $list += $e } }
        $text = $text.Substring(0, $m.Groups[1].Index) + ($list -join ',') +
            $text.Substring($m.Groups[1].Index + $m.Groups[1].Length)
    } else {
        $nl = "`n"; if ($text.Contains("`r`n")) { $nl = "`r`n" }
        if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += $nl }
        $text += "SIM_DISABLED_SENSORS=$($entries -join ',')$nl"
    }
    [System.IO.File]::WriteAllText($envFile, $text, (New-Object System.Text.UTF8Encoding($false)))
    Ok "SIM_DISABLED_SENSORS in .env includes $($entries -join ', ')"
    Push-Location $InfraDir
    try { & docker compose up -d sensor-sim; $rc = $LASTEXITCODE } finally { Pop-Location }
    if ($rc -ne 0) { Warn 'docker compose up -d sensor-sim failed; run it in imm-os-infra yourself' }
    else { Ok 'simulator restarted without them' }
}
Write-Host ''
Write-Host "Done. Open http://imm.local > Sensors: the Node health card of $NodeId should show LIVE." -ForegroundColor Green
Write-Host 'Wire sensors one at a time (real-sensors/BENCH.md), then re-run this script with -Sensors "..." to start their drivers.'
