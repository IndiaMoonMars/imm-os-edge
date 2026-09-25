"""
Vital-sign estimation from raw MAX30100/MAX30102 photoplethysmography (PPG) samples.

The sensor only reports reflected red and infrared light. Heart rate and SpO2 are
estimated from a few seconds of samples:

  heart rate  period of the IR pulse wave (autocorrelation) after removing the DC
              level and high-frequency noise
  SpO2        ratio of ratios R = (AC_red / DC_red) / (AC_ir / DC_ir), mapped with the
              common empirical calibration SpO2 ≈ 110 − 25·R

Both return None when the signal is unusable (no finger, motion, weak contact) so
callers publish nothing rather than a made-up number. These are wellness-grade
estimates for an analog habitat, not a medical device.
"""
from statistics import mean, pstdev
from typing import List, Optional, Sequence

MIN_DC = 5000          # below this there is no finger on the sensor
MIN_BPM, MAX_BPM = 35, 220


def _moving_average(x: Sequence[float], n: int) -> List[float]:
    out, acc = [], 0.0
    for i, v in enumerate(x):
        acc += v
        if i >= n:
            acc -= x[i - n]
        out.append(acc / min(i + 1, n))
    return out


def _detrend(x: Sequence[float], fs: float) -> List[float]:
    """Remove the slowly-varying DC level (1 s moving average) and smooth (~80 ms)."""
    base = _moving_average(x, max(1, int(fs)))
    ac = [v - b for v, b in zip(x, base)]
    return _moving_average(ac, max(1, int(fs * 0.08)))


def _autocorr(x: Sequence[float], lag: int) -> float:
    n = len(x) - lag
    return sum(x[i] * x[i + lag] for i in range(n)) / n


def heart_rate(ir: Sequence[float], fs: float) -> Optional[float]:
    """
    Beats per minute from ≥ 4 s of IR samples at fs Hz, or None.

    Uses the autocorrelation of the detrended pulse wave: the lag where the waveform
    best matches a shifted copy of itself is one beat. Unlike counting peaks, this is
    not fooled by the dicrotic notch (the small second bump in each PPG pulse).
    """
    if len(ir) < fs * 4 or mean(ir) < MIN_DC:
        return None
    sig = _detrend(ir, fs)[int(fs):]  # skip the filter warm-up second
    m = mean(sig)
    sig = [v - m for v in sig]
    energy = _autocorr(sig, 0)
    if energy == 0:
        return None
    lo, hi = int(fs * 60 / MAX_BPM), int(fs * 60 / MIN_BPM)
    hi = min(hi, len(sig) // 2)
    corr = {lag: _autocorr(sig, lag) / energy for lag in range(lo, hi + 1)}
    # local maxima only, so the slope right after lag lo can't win
    peaks = [lag for lag in range(lo + 1, hi) if corr[lag] >= corr[lag - 1] and corr[lag] >= corr[lag + 1]]
    if not peaks:
        return None
    best = max(peaks, key=lambda lag: corr[lag])
    # a shorter period that fits almost as well means `best` spans two beats
    for lag in peaks:
        if lag < best and abs(lag * 2 - best) <= max(2, best * 0.1) and corr[lag] > 0.8 * corr[best]:
            best = lag
            break
    if corr[best] < 0.3:   # no clear periodicity: motion or poor contact
        return None
    bpm = 60.0 * fs / best
    return round(bpm, 1) if MIN_BPM <= bpm <= MAX_BPM else None


def spo2(red: Sequence[float], ir: Sequence[float]) -> Optional[float]:
    """Oxygen saturation (%) from matching red/IR windows, or None."""
    if len(red) != len(ir) or len(ir) < 50:
        return None
    dc_r, dc_i = mean(red), mean(ir)
    if dc_r < MIN_DC or dc_i < MIN_DC:
        return None
    ac_r, ac_i = pstdev(red), pstdev(ir)
    if ac_i == 0 or ac_r == 0:
        return None
    r = (ac_r / dc_r) / (ac_i / dc_i)
    value = 110.0 - 25.0 * r
    if value < 70:        # outside the calibration's valid range: bad contact
        return None
    return round(min(value, 100.0), 1)
