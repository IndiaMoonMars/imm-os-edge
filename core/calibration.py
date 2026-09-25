"""
Per-node sensor calibration: /etc/imm-os/calibration.yaml (IMM_CALIBRATION_FILE).

Every reading a driver publishes goes through apply() (core/mqtt_publisher.stamp),
so corrections happen once, on the node, before data reaches MCC or the blackbox.

    corrected = raw * gain + offset            (gain defaults to 1, offset to 0)

File layout — sensor name → field → correction, plus who/when/against what:

    bme280:
      temp:
        offset: -0.8          # reads high: board heated by the Pi CPU
        reference: "Testo 605i, same shelf, 30 min settle"
        date: 2026-10-02
        by: pratham
      hum: {gain: 1.02, offset: -1.5, reference: "2-point salt test (33 %, 75 %)", date: 2026-10-02}
    scd40:
      co2_ppm: {offset: 12, reference: "outdoor air 420 ppm", date: 2026-10-02}

Unknown sensors/fields pass through unchanged. Set IMM_CALIBRATION_OFF=1 to see raw
(uncorrected) readings while taking reference measurements. The file is re-read automatically
when it changes, so running `tools/calibrate.py set …` takes effect without a restart.
Edit it with tools/calibrate.py rather than by hand; it validates every entry.
"""
import logging
import os
import threading
from typing import Dict, Optional

log = logging.getLogger("imm.calibration")

DEFAULT_PATH = "/etc/imm-os/calibration.yaml"
MATH_KEYS = ("gain", "offset")
INFO_KEYS = ("reference", "date", "by", "note", "raw_points", "true_points")


class CalibrationError(ValueError):
    pass


def validate(data) -> Dict[str, Dict[str, dict]]:
    """Check structure and numbers; returns the data (empty dict for an empty file)."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CalibrationError("top level must be a mapping of sensor names")
    for sensor, fields in data.items():
        if not isinstance(fields, dict):
            raise CalibrationError(f"{sensor}: must map field names to corrections")
        for field, corr in fields.items():
            if not isinstance(corr, dict):
                raise CalibrationError(f"{sensor}.{field}: must be a mapping (gain/offset/…)")
            unknown = set(corr) - set(MATH_KEYS) - set(INFO_KEYS)
            if unknown:
                raise CalibrationError(f"{sensor}.{field}: unknown key(s) {sorted(unknown)}")
            for k in MATH_KEYS:
                v = corr.get(k)
                if k in corr and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    raise CalibrationError(f"{sensor}.{field}.{k} must be a number")
            gain = corr.get("gain", 1.0)
            if not 0.5 <= gain <= 2.0:
                raise CalibrationError(f"{sensor}.{field}.gain {gain} outside 0.5–2.0: check the reference readings")
    return data


def load(path: str) -> Dict[str, Dict[str, dict]]:
    import yaml
    with open(path) as f:
        return validate(yaml.safe_load(f))


def correct_value(corr: Optional[dict], value):
    if not corr or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    out = value * corr.get("gain", 1.0) + corr.get("offset", 0.0)
    return round(out, 4)


class Calibration:
    """Loaded calibration that reloads itself when the file's mtime changes."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("IMM_CALIBRATION_FILE", DEFAULT_PATH)
        self._data: Dict[str, Dict[str, dict]] = {}
        self._mtime = None
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            if self._mtime is not None:
                log.warning("Calibration file %s removed; readings uncorrected", self.path)
            self._data, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            self._data = load(self.path)
            log.info("Loaded calibration for %s", ", ".join(sorted(self._data)) or "no sensors")
        except Exception as exc:   # keep the previous good calibration rather than none
            log.error("Ignoring invalid calibration file %s: %s", self.path, exc)
        self._mtime = mtime

    def for_sensor(self, sensor: str) -> Dict[str, dict]:
        with self._lock:
            self._refresh()
            return self._data.get(sensor, {})

    def apply(self, payload: dict) -> dict:
        """Return a copy of a reading with this node's corrections applied."""
        if os.getenv("IMM_CALIBRATION_OFF") == "1":   # read raw values while calibrating
            return payload
        fields = self.for_sensor(payload.get("sensor", ""))
        if not fields:
            return payload
        out = dict(payload)
        for field, corr in fields.items():
            if field in out:
                out[field] = correct_value(corr, out[field])
        return out

    def correct(self, sensor: str, field: str, value):
        """Correct a single value (for scripts that don't publish driver-style payloads)."""
        if os.getenv("IMM_CALIBRATION_OFF") == "1":
            return value
        return correct_value(self.for_sensor(sensor).get(field), value)


_default = None


def default() -> Calibration:
    global _default
    if _default is None:
        _default = Calibration()
    return _default
