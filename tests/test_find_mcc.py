"""tools/find_mcc.py: finding the MCC again after its IP address changes."""
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import find_mcc  # noqa: E402

IP_OUTPUT = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
3: wlan0    inet 192.168.1.135/24 brd 192.168.1.255 scope global dynamic noprefixroute wlan0\\       valid_lft 8000sec
4: eth0    inet 10.20.30.40/16 brd 10.20.255.255 scope global eth0\\       valid_lft forever
5: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
"""


def test_local_networks_skip_docker_and_narrow_to_24():
    nets = find_mcc.local_networks(IP_OUTPUT)
    assert [(i, own, str(n)) for i, own, n in nets] == [("wlan0", "192.168.1.135", "192.168.1.0/24"),
                                                         ("eth0", "10.20.30.40", "10.20.30.0/24")]


def test_candidates_previous_first_and_never_self():
    nets = find_mcc.local_networks(IP_OUTPUT)[:1]
    c = find_mcc.candidates(nets, previous="192.168.1.134")
    assert c[0] == "192.168.1.134" and "192.168.1.135" not in c and len(c) == 253
    assert c.count("192.168.1.134") == 1


def test_scan_returns_the_one_that_matches():
    assert find_mcc.scan([f"10.0.0.{i}" for i in range(1, 50)], lambda ip: ip == "10.0.0.37") == "10.0.0.37"
    assert find_mcc.scan(["10.0.0.1", "10.0.0.2"], lambda ip: False) is None


def test_set_host_updates_appends_and_keeps_the_rest(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n# 10.0.0.9 imm.local (comment)\n192.168.1.134 imm.local\n::1 localhost\n")
    assert find_mcc.set_host(str(hosts), "imm.local", "192.168.1.107")
    assert hosts.read_text() == ("127.0.0.1 localhost\n# 10.0.0.9 imm.local (comment)\n"
                                 "192.168.1.107 imm.local\n::1 localhost\n")
    assert not find_mcc.set_host(str(hosts), "imm.local", "192.168.1.107")          # already right
    tmpl = tmp_path / "hosts.debian.tmpl"
    tmpl.write_text("## template:jinja\n127.0.1.1 {{fqdn}} {{hostname}}\n")
    assert find_mcc.set_host(str(tmpl), "imm.local", "192.168.1.107")
    assert tmpl.read_text().endswith("127.0.1.1 {{fqdn}} {{hostname}}\n192.168.1.107 imm.local\n")


def test_discover_keeps_a_working_address_and_repoints_a_stale_one(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n192.168.1.134 imm.local\n")
    nets = find_mcc.local_networks(IP_OUTPUT)[:1]
    logs = []
    mcc_at = {"ip": "192.168.1.134"}
    check = lambda ip, name, port, ca: ip == mcc_at["ip"]   # noqa: E731
    kw = dict(check=check, resolver=lambda n: "192.168.1.134", hosts_files=[str(hosts)], log=logs.append)
    assert find_mcc.discover("imm.local", 8883, "ca.crt", nets, **kw) == 0
    assert logs == [] and "192.168.1.134 imm.local" in hosts.read_text()          # working: silent, unchanged
    mcc_at["ip"] = "192.168.1.107"                                                  # the PC got a new address
    assert find_mcc.discover("imm.local", 8883, "ca.crt", nets, **kw) == 0
    assert "192.168.1.107 imm.local" in hosts.read_text()
    assert "MCC found at 192.168.1.107 (was 192.168.1.134)" in logs[-1]
    mcc_at["ip"] = None                                                             # PC gone
    assert find_mcc.discover("imm.local", 8883, "ca.crt", nets, **kw) == 1
    assert "MCC not found" in logs[-1]


# ── against a real TLS server with an IMM-style CA ─────────────────

def _openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("pki")
    for ca in ("ca", "otherca"):
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={ca}",
                 "-keyout", str(d / f"{ca}.key"), "-out", str(d / f"{ca}.crt"))
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=imm.local",
             "-keyout", str(d / "server.key"), "-out", str(d / "server.csr"))
    (d / "ext.cnf").write_text("subjectAltName=DNS:imm.local,DNS:mosquitto\n")
    _openssl("x509", "-req", "-in", str(d / "server.csr"), "-CA", str(d / "ca.crt"), "-CAkey", str(d / "ca.key"),
             "-CAcreateserial", "-days", "2", "-extfile", str(d / "ext.cnf"), "-out", str(d / "server.crt"))
    return d


@pytest.fixture
def tls_server(pki):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pki / "server.crt"), str(pki / "server.key"))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    srv.close()


def test_is_mcc_against_a_real_tls_server(pki, tls_server):
    ca, other = str(pki / "ca.crt"), str(pki / "otherca.crt")
    assert find_mcc.is_mcc("127.0.0.1", "imm.local", tls_server, ca)
    assert not find_mcc.is_mcc("127.0.0.1", "imm.local", tls_server, other)        # someone else's CA
    assert not find_mcc.is_mcc("127.0.0.1", "evil.local", tls_server, ca)          # certificate for another name
    free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
    assert not find_mcc.is_mcc("127.0.0.1", "imm.local", port, ca, timeout=0.5)    # nothing listening


def test_config_reads_edge_env(tmp_path, monkeypatch):
    for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_TLS_CA"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "edge.env"
    env.write_text("MQTT_HOST=imm.local\nMQTT_PORT=8883\nMQTT_TLS_CA=/etc/imm-os/mqtt-ca.crt\nMQTT_PASSWORD=x\n")
    assert find_mcc.config(str(env)) == ("imm.local", 8883, "/etc/imm-os/mqtt-ca.crt")
