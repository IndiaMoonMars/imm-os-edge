"""
Parsers for EVA positioning hardware.

  NMEA 0183 (u-blox NEO-M9N GPS): RMC for position/validity, GGA for fix quality,
  satellites and altitude. The M9N is multi-constellation, so sentences start with
  $GN (not $GP); any talker ID is accepted. Sentences with a bad checksum are dropped.

  Decawave/Qorvo DWM1001 tag, UART shell `lec` output: one CSV line per update, e.g.
    DIST,4,AN0,1151,5.00,8.00,2.25,6.48,AN1,...,POS,1.23,4.56,0.87,85
  The POS block is the tag position (metres, in the anchors' frame) and a quality
  factor 0–100.
"""
from typing import Optional


def nmea_checksum_ok(sentence: str) -> bool:
    s = sentence.strip()
    if not s.startswith("$") or "*" not in s:
        return False
    body, _, given = s[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value: str, hemi: str) -> Optional[float]:
    """ddmm.mmmm / dddmm.mmmm + N/S/E/W → signed decimal degrees."""
    if not value:
        return None
    raw = float(value)
    deg = int(raw / 100)
    out = deg + (raw - deg * 100) / 60.0
    return -out if hemi in ("S", "W") else out


def parse_nmea(sentence: str) -> Optional[dict]:
    """
    Parse one RMC or GGA sentence. Returns a dict of the fields present, or None for
    other sentence types, bad checksums, or 'no fix'.
      RMC → {"type": "RMC", "lat", "lon", "speed_kn", "course_deg"}
      GGA → {"type": "GGA", "lat", "lon", "fix_quality", "satellites", "hdop", "alt_m"}
    """
    if not nmea_checksum_ok(sentence):
        return None
    fields = sentence.strip()[1:].split("*")[0].split(",")
    kind = fields[0][2:]
    try:
        if kind == "RMC" and len(fields) >= 9:
            if fields[2] != "A":          # V = receiver warning, no valid fix
                return None
            return {"type": "RMC", "lat": _coord(fields[3], fields[4]), "lon": _coord(fields[5], fields[6]),
                    "speed_kn": float(fields[7] or 0), "course_deg": float(fields[8] or 0)}
        if kind == "GGA" and len(fields) >= 10:
            quality = int(fields[6] or 0)
            if quality == 0:
                return None
            return {"type": "GGA", "lat": _coord(fields[2], fields[3]), "lon": _coord(fields[4], fields[5]),
                    "fix_quality": quality, "satellites": int(fields[7] or 0),
                    "hdop": float(fields[8] or 0), "alt_m": float(fields[9] or 0)}
    except ValueError:
        return None
    return None


def parse_dwm_lec(line: str) -> Optional[dict]:
    """Extract the POS block from a DWM1001 `lec` line → {x_m, y_m, z_m, quality}, or None."""
    parts = [p.strip() for p in line.strip().split(",")]
    if "POS" not in parts:
        return None
    i = parts.index("POS")
    if len(parts) < i + 5:
        return None
    try:
        x, y, z, q = (float(parts[i + 1]), float(parts[i + 2]), float(parts[i + 3]), int(float(parts[i + 4])))
    except ValueError:
        return None
    if any(v != v for v in (x, y, z)):   # NaN: the tag has no position yet
        return None
    return {"x_m": x, "y_m": y, "z_m": z, "quality": max(0, min(100, q))}
