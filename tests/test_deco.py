"""The tissue model behind the calculated surface GF."""

from bottomtime.deco import _run, _surface_state


def _square(depth_m, minutes, o2=21, mode="oc", ppo2=None):
    t = list(range(0, minutes * 60 + 1, 10))
    depths = [depth_m] * len(t) + [0.0] * 30
    t += [t[-1] + 10 * (i + 1) for i in range(30)]
    n = len(t)
    return {"header": {"atmospheric_mbar": 1013, "water_density": 1030}, "mode": mode,
            "samples": {"t_s": t, "depth_m": depths, "o2_pct": [o2] * n, "he_pct": [0] * n, "avg_ppo2": [ppo2] * n}}


def test_a_square_dive_on_air_leaves_a_surface_gf_that_falls_after_surfacing():
    _, out = _run(_square(30, 25), _surface_state(1.013))
    assert out[0][1] < 0  # saturated at the surface, so nothing to off-gas
    at_bottom_end = out[150][1]
    assert at_bottom_end > 60  # 25 minutes at 30 m on air
    assert out[-1][1] < at_bottom_end  # off-gassing at the surface


def test_a_rebreather_at_a_high_setpoint_loads_less_than_air():
    _, air = _run(_square(30, 25), _surface_state(1.013))
    _, ccr = _run(_square(30, 25, mode="cc", ppo2=1.3), _surface_state(1.013))
    assert ccr[150][1] < air[150][1]


def test_the_computers_own_tissue_snapshot_is_read_as_16_nitrogen_and_16_helium():
    import sqlite3
    import struct

    from bottomtime.deco import first_snapshot

    values = [0.75 + i / 100 for i in range(16)] + [0.001 * (i + 1) for i in range(16)] + [0.0] * 9
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE undecoded_payloads (source_dive_id, container, type_key, seq, payload)")
    for k in range(6):
        floats = values[k * 7:k * 7 + (6 if k == 5 else 7)]
        payload = bytes([0x70 + k, 0, 0, 0]) + struct.pack("<%df" % len(floats), *floats)
        payload += struct.pack(">I", 1783338232) if k == 5 else b""
        con.execute("INSERT INTO undecoded_payloads VALUES (1, 'pnf_record', ?, ?, ?)", (f"0x{0x70 + k:x}", 10 + k, payload))
    n2, he = first_snapshot(con, 1)
    assert [round(v, 3) for v in n2] == [round(v, 3) for v in values[:16]]
    assert [round(v, 4) for v in he] == [round(v, 4) for v in values[16:32]]
    assert first_snapshot(con, 2) is None
