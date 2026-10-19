"""Comparing two computers' logs of one dive."""

from bottomtime.api import compare_logs, depth_gap


def _log(model, density, depths, step_s, offset_s=0.0, source="shearwater", device=None):
    t = [i * step_s for i in range(len(depths))]
    samples = {"t_s": t, "depth_m": depths, "cns_pct": [5] * len(depths)}
    if source == "shearwater":
        samples["in_deco"] = [0] * len(depths)
    else:
        samples["next_stop_depth_m"] = [0] * len(depths)
    return {
        "device": device or model,
        "offset_s": offset_s,
        "sample_interval_ms": step_s * 1000,
        "max_depth_m": max(depths),
        "duration_s": t[-1],
        "samples": samples,
        "computer": {"model": model, "water_density": density, "gf_low": 50, "gf_high": 70, "deco_model": "gf"},
    }


def test_a_lighter_water_setting_reads_deeper():
    profile = [min(i, 30) for i in range(60)]
    shallow = _log("Perdix 2", 1020, profile, 10)
    deep = _log("Descent Mk3", 1000, [d * 1.02 for d in profile], 10, source="garmin")
    c = compare_logs({"sources": {"shearwater": [shallow], "garmin": [deep]}})
    gap = c["depth"][0]
    assert abs(gap["pct"] - 2.0) < 0.01
    assert abs(gap["settings_pct"] - 2.0) < 0.01  # 1020/1000 explains all of it
    assert gap["m"] > 0


def test_readings_are_paired_on_the_shared_timeline():
    a = ([0.0, 10.0, 20.0], [10.0, 10.0, 10.0])
    b_far = ([100.0, 110.0, 120.0], [11.0, 11.0, 11.0])
    assert depth_gap(a, b_far) is None  # nothing within 1.5 s
    a = ([float(t) for t in range(0, 300, 10)], [10.0] * 30)
    b = ([t + 1.0 for t in a[0]], [9.0] * 30)
    assert depth_gap(a, b)["pct"] == -10.0


def test_one_log_has_nothing_to_compare():
    assert compare_logs({"sources": {"garmin": [_log("Descent Mk3", 1025, [10] * 30, 1, source="garmin")]}}) is None


def test_a_computer_split_into_several_logs_is_one_computer():
    # The Perdix ended a dive at the surface halfway; the Descent kept going.
    first = _log("Perdix 2", 1020, [min(i, 20) for i in range(30)], 10)
    second = _log("Perdix 2", 1020, [min(i, 20) for i in range(30)], 10, offset_s=400)
    descent = _log("Descent Mk3", 1000, [min(i % 40, 20) * 1.02 for i in range(70)], 10, source="garmin")
    c = compare_logs({"sources": {"garmin": [descent], "shearwater": [second, first]}})
    assert [(r["computer"], r["logs"]) for r in c["logs"]] == [("Descent Mk3", 1), ("Perdix 2", 2)]
    assert c["logs"][1]["duration_s"] == 2 * 290
    assert len(c["depth"]) == 1
