#!/usr/bin/env python3
"""
Keep `imm.local` pointing at the MCC when the MCC PC's IP address changes.

Edge nodes reach the MCC by name (MCC_HOST, normally imm.local), which setup-node.sh
maps to the PC's LAN address in /etc/hosts. A laptop MCC gets a new address from DHCP,
or moves to another Wi-Fi network. This check runs every minute (imm-mcc-discovery.timer):

  1. If MQTT_HOST answers a TLS handshake on MQTT_PORT with a certificate for that name,
     signed by this node's MQTT CA (MQTT_TLS_CA), nothing changes.
  2. Otherwise it scans the node's local IPv4 network(s) (each narrowed to at most a /24)
     for the host that passes the same check, and points the name at it in /etc/hosts and
     in cloud-init's hosts templates (Raspberry Pi OS rewrites /etc/hosts from those at
     boot). Only a machine holding the MCC's certificate matches, so no other device can
     take its place.

The local broker's bridge, the drivers' MQTT clients (--direct nodes) and the Keycloak
login resolve the name again on their next reconnect or request, so they follow on their own.

  sudo .venv/bin/python tools/find_mcc.py            # check, rescan if needed
  sudo .venv/bin/python tools/find_mcc.py --scan     # rescan even if the current address works
Exit status: 0 MCC reachable (after a fix if needed), 1 not found, 2 not configured.
"""
import argparse
import glob
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envfile  # noqa: E402

ENV_FILE = os.getenv("IMM_ENV_FILE", "/etc/imm-os/edge.env")
HOSTS_FILE = os.getenv("IMM_HOSTS_FILE", "/etc/hosts")
CLOUD_TEMPLATES = os.getenv("IMM_CLOUD_HOSTS_TEMPLATES", "/etc/cloud/templates")
SKIP_IFACES = ("lo", "docker", "br-", "veth", "virbr", "tailscale", "wg")


def is_mcc(ip: str, name: str, port: int, ca: str, timeout: float = 1.5) -> bool:
    """Does ip:port complete a TLS handshake with a certificate for `name` signed by `ca`?"""
    try:
        ctx = ssl.create_default_context(cafile=ca)
        with socket.create_connection((ip, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=name):
                return True
    except (OSError, ssl.SSLError, ValueError):
        return False


def resolve(name: str):
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def local_networks(ip_addr_output: str):
    """`ip -4 -o addr show scope global` → [(iface, own_ip, network)], each network at most a /24."""
    nets = []
    for line in ip_addr_output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1].rstrip(":")
        if iface.startswith(SKIP_IFACES):
            continue
        itf = ipaddress.ip_interface(parts[3])
        net = itf.network if itf.network.prefixlen >= 24 else ipaddress.ip_interface(f"{itf.ip}/24").network
        nets.append((iface, str(itf.ip), net))
    return nets


def candidates(nets, previous=None):
    """Every other host on the node's networks; the previous MCC address first."""
    out = [previous] if previous else []
    for _, own, net in nets:
        out += [str(h) for h in net.hosts() if str(h) != own and str(h) != previous]
    return out


def scan(ips, check, workers: int = 64):
    """First address for which check(ip) is true, or None."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check, ip): ip for ip in ips}
        for fut in as_completed(futures):
            if fut.result():
                for other in futures:
                    other.cancel()
                return futures[fut]
    return None


def set_host(path: str, name: str, ip: str) -> bool:
    """Point `name` at `ip` in a hosts-format file (update its line or append one). True if changed."""
    with open(path) as f:
        lines = f.read().splitlines()
    out, found, changed = [], False, False
    for line in lines:
        fields = line.split()
        if fields and not fields[0].startswith("#") and name in fields[1:]:
            try:
                ipaddress.IPv4Address(fields[0])
            except ValueError:
                out.append(line)
                continue
            found = True
            if fields[0] != ip:
                line = line.replace(fields[0], ip, 1)
                changed = True
        out.append(line)
    if not found:
        out.append(f"{ip} {name}")
        changed = True
    if changed:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    return changed


def config(env_file: str = ENV_FILE):
    lines = open(env_file).read().splitlines() if os.path.exists(env_file) else []
    get = lambda k, d=None: os.getenv(k) or envfile.get(lines, k) or d   # noqa: E731
    # MCC_* (local broker mode: MQTT_* then point at localhost), else the older MQTT_* keys
    return (get("MCC_HOST") or get("MQTT_HOST", "imm.local"), int(get("MCC_MQTT_PORT") or get("MQTT_PORT", "8883")),
            get("MCC_TLS_CA") or get("MQTT_TLS_CA"))


def discover(name, port, ca, networks, force=False, check=is_mcc, resolver=resolve,
             hosts_files=None, log=print) -> int:
    current = resolver(name)
    if current and not force and check(current, name, port, ca):
        return 0
    log(f"{name} ({current or 'unresolved'}) does not answer as the MCC; scanning "
        + ", ".join(f"{n} on {i}" for i, _, n in networks))
    found = scan(candidates(networks, current), lambda ip: check(ip, name, port, ca))
    if not found:
        log("MCC not found: is the MCC PC on this network, with the stack running and TCP 8883 allowed "
            "(Windows: network marked Private)?")
        return 1
    files = hosts_files if hosts_files is not None else \
        [HOSTS_FILE] + sorted(glob.glob(os.path.join(CLOUD_TEMPLATES, "hosts.*.tmpl")))
    changed = [f for f in files if set_host(f, name, found)]
    log(f"MCC found at {found}" + (f" (was {current}); updated {', '.join(changed)}" if changed else ""))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", action="store_true", help="rescan even if the current address works")
    args = parser.parse_args()
    name, port, ca = config()
    if not ca or not os.path.exists(ca):
        print(f"MQTT_TLS_CA not set or missing in {ENV_FILE}: run scripts/setup-node.sh first")
        sys.exit(2)
    try:
        ipaddress.IPv4Address(name)
        print(f"MCC_HOST is an IP address ({name}); nothing to discover")
        sys.exit(0)
    except ValueError:
        pass
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    sys.exit(discover(name, port, ca, local_networks(out), force=args.scan))


if __name__ == "__main__":
    main()
