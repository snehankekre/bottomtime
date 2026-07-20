"""Depth-profile cross-correlation for dual-computer dive matching.

Both series are resampled onto a common 10 s grid in memory only (stored
samples are never resampled) and compared with normalized cross-correlation
over a window of candidate lags.
"""

from __future__ import annotations

GRID_S = 10.0


def resample(points: list[tuple[float, float]], t0: float, t1: float) -> list[float]:
    """Linear interpolation of (t, depth) points onto the common grid."""
    if not points:
        return []
    out = []
    i = 0
    t = t0
    while t <= t1:
        while i + 1 < len(points) and points[i + 1][0] < t:
            i += 1
        if t <= points[0][0]:
            out.append(points[0][1])
        elif t >= points[-1][0]:
            out.append(points[-1][1])
        else:
            (ta, da), (tb, db) = points[i], points[i + 1]
            out.append(da if tb == ta else da + (db - da) * (t - ta) / (tb - ta))
        t += GRID_S
    return out


def _ncc(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 6:
        return 0.0
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def median_abs_delta(
    series_a: list[tuple[float, float]],
    series_b: list[tuple[float, float]],
    lag_s: float = 0.0,
) -> float | None:
    """Median |depth difference| over the overlapping window, with series_b
    shifted by lag_s. Robust where NCC is unstable (low-variance pool
    profiles): two computers on the same body must agree on depth."""
    if not series_a or not series_b:
        return None
    shifted = [(t + lag_s, d) for t, d in series_b]
    t0 = max(series_a[0][0], shifted[0][0])
    t1 = min(series_a[-1][0], shifted[-1][0])
    if t1 - t0 < 3 * GRID_S:
        return None
    grid_a = resample(series_a, t0, t1)
    grid_b = resample(shifted, t0, t1)
    deltas = sorted(abs(a - b) for a, b in zip(grid_a, grid_b))
    return deltas[len(deltas) // 2] if deltas else None


def best_lag(
    series_a: list[tuple[float, float]],
    series_b: list[tuple[float, float]],
    max_lag_s: float = 300.0,
) -> tuple[float, float]:
    """Return (lag_s, score): shift applied to series_b that maximizes the
    normalized cross-correlation with series_a. Times are absolute seconds
    on a shared clock."""
    if not series_a or not series_b:
        return 0.0, 0.0
    t0 = max(series_a[0][0], series_b[0][0]) - max_lag_s
    t1 = min(series_a[-1][0], series_b[-1][0]) + max_lag_s
    if t1 <= t0:
        return 0.0, 0.0

    grid_a = resample(series_a, t0, t1)
    best = (0.0, -2.0)
    lag = -max_lag_s
    while lag <= max_lag_s:
        shifted = [(t + lag, d) for t, d in series_b]
        grid_b = resample(shifted, t0, t1)
        score = _ncc(grid_a, grid_b)
        if score > best[1]:
            best = (lag, score)
        lag += GRID_S
    return best
