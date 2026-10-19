"""Surface GF for Shearwater logs, which the computer shows during a dive but
doesn't record.

bottomtime rebuilds the computer's Bühlmann ZHL-16C tissue loading from the
log's depth, gas, and (on a rebreather) loop ppO2. It starts from the tissue
state the computer itself wrote at the start of the log (PNF records 0x70 to
0x75), or, for a log without one, carries it over from the same computer's
dives in the 48 hours before. The GF99 that tissue state implies is compared
with the GF99 the computer logged, so every dive says how well the model
agreed with the computer.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta
from statistics import median

# ZHL-16C, compartment 1 as 1b: (half-time in minutes, a, b) for N2 and He.
N2 = [(5.0, 1.1696, 0.5578), (8.0, 1.0, 0.6514), (12.5, 0.8618, 0.7222), (18.5, 0.7562, 0.7825),
      (27.0, 0.62, 0.8126), (38.3, 0.5043, 0.8434), (54.3, 0.441, 0.8693), (77.0, 0.4, 0.891),
      (109.0, 0.375, 0.9092), (146.0, 0.35, 0.9222), (187.0, 0.3295, 0.9319), (239.0, 0.3065, 0.9403),
      (305.0, 0.2835, 0.9477), (390.0, 0.261, 0.9544), (498.0, 0.248, 0.9602), (635.0, 0.2327, 0.9653)]
HE = [(1.88, 1.6189, 0.477), (3.02, 1.383, 0.5747), (4.72, 1.1919, 0.6527), (6.99, 1.0458, 0.7223),
      (10.21, 0.922, 0.7582), (14.48, 0.8205, 0.7957), (20.53, 0.7305, 0.8279), (29.11, 0.6502, 0.8553),
      (41.2, 0.595, 0.8757), (55.19, 0.5545, 0.8903), (70.69, 0.5333, 0.8997), (90.34, 0.5189, 0.9073),
      (115.29, 0.5181, 0.9122), (147.42, 0.5176, 0.9171), (188.24, 0.5172, 0.9217), (240.03, 0.5119, 0.9267)]
WATER_VAPOUR_BAR = 0.0627
AIR_N2 = 0.7902
CARRY_OVER = timedelta(hours=48)


def _surface_state(p_surf: float) -> tuple[list[float], list[float]]:
    return [(p_surf - WATER_VAPOUR_BAR) * AIR_N2] * 16, [0.0] * 16


def _rest(state, minutes: float, p_surf: float):
    """Tissues after breathing air at the surface for some minutes."""
    pn, ph = state
    air = (p_surf - WATER_VAPOUR_BAR) * AIR_N2
    return ([p + (air - p) * (1 - 2 ** (-minutes / N2[i][0])) for i, p in enumerate(pn)],
            [p * 2 ** (-minutes / HE[i][0]) for i, p in enumerate(ph)])


def _gf(pn: list[float], ph: list[float], p_amb: float) -> float:
    """The leading compartment's supersaturation at p_amb, as a percentage
    of its M-value gradient there."""
    worst = float("-inf")
    for i in range(16):
        pt = pn[i] + ph[i]
        a = (N2[i][1] * pn[i] + HE[i][1] * ph[i]) / pt
        b = (N2[i][2] * pn[i] + HE[i][2] * ph[i]) / pt
        m = a + p_amb / b
        worst = max(worst, (pt - p_amb) / (m - p_amb) * 100)
    return worst


def _run(log: dict, state):
    """Load tissues through one log. Returns the end state and, per sample,
    (model GF99, model surface GF)."""
    h, s = log["header"], log["samples"]
    p_surf = (h.get("atmospheric_mbar") or 1013) / 1000
    rho = h.get("water_density") or 1030
    pn, ph = list(state[0]), list(state[1])
    cc = log["mode"] == "cc"
    out, prev = [], None
    for t, d, o2, he, ppo2 in zip(s["t_s"], s["depth_m"], s["o2_pct"], s["he_pct"], s["avg_ppo2"]):
        p = p_surf + (d or 0) * rho * 9.80665 / 1e5
        if prev is not None:
            minutes, p_mid = (t - prev[0]) / 60, (p + prev[1]) / 2
            fn2, fhe = 1 - (o2 or 21) / 100 - (he or 0) / 100, (he or 0) / 100
            if cc and ppo2:
                # The loop holds ppO2, and the diluent's inert gases share the rest.
                inert = max(p_mid - WATER_VAPOUR_BAR - ppo2, 0)
                in_n2, in_he = inert * fn2 / (fn2 + fhe), inert * fhe / (fn2 + fhe)
            else:
                in_n2, in_he = (p_mid - WATER_VAPOUR_BAR) * fn2, (p_mid - WATER_VAPOUR_BAR) * fhe
            for i in range(16):
                pn[i] += (in_n2 - pn[i]) * (1 - 2 ** (-minutes / N2[i][0]))
                ph[i] += (in_he - ph[i]) * (1 - 2 ** (-minutes / HE[i][0]))
        prev = (t, p)
        out.append((_gf(pn, ph, p), _gf(pn, ph, p_surf)))
    return (pn, ph), out


def first_snapshot(con: sqlite3.Connection, source_dive_id: int):
    """The tissue state a Shearwater wrote before its first sample, as
    (16 nitrogen, 16 helium) pressures in bar, or None. A snapshot is six
    records, 0x70 to 0x75, holding little-endian floats from byte 4: seven in
    each of the first five records and six in the last, whose last four bytes
    are a big-endian timestamp. The first 16 floats are nitrogen and the next
    16 helium. The nine after that aren't known yet."""
    rows = con.execute(
        "SELECT type_key, payload FROM undecoded_payloads WHERE source_dive_id=? AND container='pnf_record'"
        " AND type_key IN ('0x70','0x71','0x72','0x73','0x74','0x75') ORDER BY seq LIMIT 6",
        (source_dive_id,),
    ).fetchall()
    if [k for k, _ in rows] != ["0x70", "0x71", "0x72", "0x73", "0x74", "0x75"]:
        return None
    values = []
    for k, payload in rows:
        values += struct.unpack("<6f" if k == "0x75" else "<7f", payload[4:28 if k == "0x75" else 32])
    return values[0:16], values[16:32]


def _log(con: sqlite3.Connection, source_dive_id: int) -> dict:
    sd = con.execute(
        "SELECT start_time_local, mode, header_json FROM source_dives WHERE id=?", (source_dive_id,)
    ).fetchone()
    rows = con.execute(
        "SELECT t_s, depth_m, o2_pct, he_pct, avg_ppo2, gf99 FROM shearwater_samples"
        " WHERE source_dive_id=? ORDER BY t_s", (source_dive_id,)
    ).fetchall()
    cols = ["t_s", "depth_m", "o2_pct", "he_pct", "avg_ppo2", "gf99"]
    return {
        "start": datetime.fromisoformat(sd[0]),
        "mode": sd[1],
        "header": json.loads(sd[2]).get("pnf") or {},
        "samples": {c: [r[i] for r in rows] for i, c in enumerate(cols)},
    }


def surface_gf(db_path, source_dive_id: int) -> dict | None:
    """Surface GF for every sample of a Shearwater log, and how far the
    model's GF99 was from the logged GF99 (the median, in points, over
    samples where the computer logged 5 or more). None under VPM-B or DCIEM,
    whose computers don't use gradient factors."""
    con = sqlite3.connect(db_path)
    try:
        log = _log(con, source_dive_id)
        if log["header"].get("deco_model") not in (None, "gf"):
            return None
        own = first_snapshot(con, source_dive_id)
        if own:
            _, out = _run(log, own)
            return _result(out, log)
        device = con.execute("SELECT device FROM source_dives WHERE id=?", (source_dive_id,)).fetchone()[0]
        earlier = con.execute(
            "SELECT id FROM source_dives WHERE device=? AND start_time_local < ? AND start_time_local >= ?"
            " ORDER BY start_time_local",
            (device, log["start"].isoformat(sep=" "), (log["start"] - CARRY_OVER).isoformat(sep=" ")),
        ).fetchall()
        p_surf = (log["header"].get("atmospheric_mbar") or 1013) / 1000
        state, end = _surface_state(p_surf), None
        for (other,) in earlier:
            prior = _log(con, other)
            if end is not None:
                state = _rest(state, (prior["start"] - end).total_seconds() / 60, p_surf)
            state, _ = _run(prior, state)
            end = prior["start"] + timedelta(seconds=prior["samples"]["t_s"][-1] if prior["samples"]["t_s"] else 0)
        if end is not None:
            state = _rest(state, max((log["start"] - end).total_seconds() / 60, 0), p_surf)
        _, out = _run(log, state)
    finally:
        con.close()
    return _result(out, log)


def _result(out, log) -> dict:
    gaps = [abs(m - g) for (m, _), g in zip(out, log["samples"]["gf99"]) if g is not None and g >= 5]
    return {
        "surface_gf": [round(max(sf, 0), 1) for _, sf in out],
        "gf99_gap": round(median(gaps), 1) if gaps else None,
        "gf99_readings": len(gaps),
    }
