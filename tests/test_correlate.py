from bottomtime.match.correlate import best_lag, median_abs_delta


def _profile(t0: float, n: int = 120, step: float = 10.0):
    """A dive-like depth profile: descend, bottom, ascend."""
    out = []
    for i in range(n):
        t = t0 + i * step
        if i < 20:
            depth = i * 1.5
        elif i < 90:
            depth = 30.0 + (i % 7) * 0.3
        else:
            depth = max(0.0, 30.0 - (i - 90) * 1.2)
        out.append((t, depth))
    return out


def test_best_lag_recovers_shift():
    a = _profile(1000.0)
    b = [(t - 40.0, d) for t, d in a]  # b's clock runs 40 s behind
    lag, score = best_lag(a, b)
    assert lag == 40.0
    assert score > 0.99


def test_best_lag_rejects_unrelated():
    a = _profile(1000.0)
    b = [(t, 3.0 + (i % 3) * 0.1) for i, (t, _) in enumerate(a)]
    _lag, score = best_lag(a, b)
    assert score < 0.5


def test_median_abs_delta():
    a = _profile(0.0)
    b = [(t, d + 0.3) for t, d in a]
    delta = median_abs_delta(a, b)
    assert delta is not None
    assert abs(delta - 0.3) < 0.01
