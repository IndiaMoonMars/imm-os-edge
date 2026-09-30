#!/usr/bin/env python3
"""
Keycloak client-credentials helper for edge nodes (RPi / Jetson).

Edge scripts calling IMM-OS HTTP APIs (ECLSS event logs, EVA RFID station,
telemetry ingest) authenticate as the `imm-edge` client, whose service account
carries the `edge_device` role. Tokens are cached and refreshed shortly
before they expire.

Environment (put secrets in /etc/imm-os/edge.env, see systemd/edge.env.example):
    KEYCLOAK_TOKEN_URL      token endpoint; use the same public host the backend
                            expects in the token issuer (default imm.local)
    IMM_EDGE_CLIENT_ID      default: imm-edge
    IMM_EDGE_CLIENT_SECRET  from Keycloak (Clients → imm-edge → Credentials)

Usage:
    requests.post(url, json=payload, headers=auth_headers(), timeout=5)
"""
import logging
import os
import threading
import time

import requests

log = logging.getLogger("auth_client")

TOKEN_URL = os.getenv(
    "KEYCLOAK_TOKEN_URL",
    "http://imm.local/auth/realms/IndiaMoonMars/protocol/openid-connect/token")
CLIENT_ID = os.getenv("IMM_EDGE_CLIENT_ID", "imm-edge")
REFRESH_MARGIN_S = 30

_lock = threading.Lock()
_token = None
_expires_at = 0.0
_warned_unconfigured = False


def _fetch_token(secret: str):
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, secret),
        timeout=5,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], time.time() + float(body.get("expires_in", 60))


def auth_headers() -> dict:
    """Authorization header for IMM-OS APIs, or {} if no token could be obtained."""
    global _token, _expires_at, _warned_unconfigured
    secret = os.getenv("IMM_EDGE_CLIENT_SECRET", "")
    if not secret:
        if not _warned_unconfigured:
            log.error("IMM_EDGE_CLIENT_SECRET is not set; API calls will be rejected (401)")
            _warned_unconfigured = True
        return {}
    with _lock:
        if _token is None or time.time() >= _expires_at - REFRESH_MARGIN_S:
            try:
                _token, _expires_at = _fetch_token(secret)
            except (requests.RequestException, KeyError, ValueError) as e:
                log.error(f"Could not obtain edge token from {TOKEN_URL}: {e}")
                _token, _expires_at = None, 0.0
                return {}
        return {"Authorization": f"Bearer {_token}"}
