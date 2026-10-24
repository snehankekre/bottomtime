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


def _snapshot_rows(con, source_dive_id, seq, ts, n2, he):
    import struct

    values = list(n2) + list(he) + [0.0] * 9
    for k in range(6):
        floats = values[k * 7:k * 7 + (6 if k == 5 else 7)]
        payload = bytes([0x70 + k, 0, 0, 0]) + struct.pack("<%df" % len(floats), *floats)
        payload += struct.pack(">I", ts) if k == 5 else b""
        con.execute("INSERT INTO undecoded_payloads (source_dive_id, container, type_key, seq, payload) VALUES (?, 'pnf_record', ?, ?, ?)",
                    (source_dive_id, f"0x{0x70 + k:x}", seq + k, payload))


def test_tissues_start_again_from_each_snapshot_the_computer_wrote(tmp_path):
    import json
    import sqlite3

    from bottomtime.deco import tissues

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE source_dives (id, start_time_local, mode, header_json)")
    con.execute("CREATE TABLE shearwater_samples (source_dive_id, t_s, depth_m, o2_pct, he_pct, avg_ppo2, gf99)")
    con.execute("CREATE TABLE undecoded_payloads (source_dive_id, container, type_key, seq, payload)")
    start = 1_760_000_000
    header = {"pnf": {"start_time": start, "atmospheric_mbar": 1013, "water_density": 1030, "deco_model": "gf",
                      "gf_low": 50, "gf_high": 80}}
    con.execute("INSERT INTO source_dives VALUES (1, '2025-10-01 10:00:00', 'oc_rec', ?)", (json.dumps(header),))
    for t in range(0, 601, 10):
        con.execute("INSERT INTO shearwater_samples VALUES (1, ?, 20.0, 21, 0, NULL, NULL)", (t,))
    surface = [(1.013 - 0.0627) * 0.7902] * 16
    _snapshot_rows(con, 1, 0, start, surface, [0.0] * 16)
    # The computer's second snapshot, at 240 s, says something the model wouldn't.
    _snapshot_rows(con, 1, 100, start + 240, [2.0] * 16, [0.5] * 16)
    con.commit()
    con.close()

    out = tissues(db, 1)
    at_240 = 24
    assert out["n2"][at_240] == [2.0] * 16 and out["he"][at_240] == [0.5] * 16
    assert out["snapshots_s"] == [0, 240]
    assert out["drift_checks"] == 1 and out["drift_bar"] > 0.4  # the model hadn't put any helium in
    # It carries on from the snapshot: on air at 20 m the helium it was given
    # washes out, and nitrogen keeps rising toward the inspired 2.34 bar.
    assert 0 < out["he"][-1][0] < 0.5
    assert 2.0 < out["n2"][-1][0] < 2.35
    assert len(out["gf"]) == len(out["p_amb"]) == 61
