"""Verification suite for the dive store.

1. PNF-vs-XML: the Shearwater XML export is an independent rendering of the
   same native log; per-sample equality on shared channels validates the PNF
   decoder byte offsets.
2. gf99 sanity: max gf99 near surfacing must agree with Shearwater's own
   EndGF99; ceiling must never exceed the (3 m-quantized) stop depth.
3. Twin-dive agreement: matched Garmin/Shearwater series must show the same
   depth profile after clock offset + residual skew correction.
4. Counts and referential integrity.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..ingest.shearwater_xml import load_xml_dir

TOL = {
    "depth_m": 0.051,
    "tts_min": 0.001,
    "stop_depth_m": 0.11,
    "o2_pct": 0.001,
    "he_pct": 0.001,
    "avg_ppo2": 0.011,
    "temp_c": 0.51,
    "battery_v": 0.011,
    "ndl_min": 0.001,
}


def _fail(failures: list, msg: str):
    failures.append(msg)
    print(f"FAIL {msg}")


def check_xml(con: sqlite3.Connection, xml_dir: Path, failures: list) -> None:
    xml_dives = load_xml_dir(xml_dir)
    checked = worst = 0
    worst_field = ""
    for start, parsed in sorted(xml_dives.items()):
        number = parsed["header"].get("number", "?")
        row = con.execute(
            "SELECT id FROM source_dives WHERE source='shearwater' AND start_time_local=?",
            (start,),
        ).fetchone()
        if row is None:
            _fail(failures, f"xml dive #{number} ({start}): no matching shearwater source dive")
            continue
        samples = con.execute(
            "SELECT * FROM shearwater_samples WHERE source_dive_id=? ORDER BY t_s",
            (row[0],),
        ).fetchall()
        xs = parsed["samples"]
        if abs(len(samples) - len(xs)) > 8:
            _fail(
                failures,
                f"dive #{number}: sample count {len(samples)} vs xml {len(xs)}",
            )
            continue
        pairs = [
            ("currentDepth", "depth_m", 1.0),
            ("ttsMins", "tts_min", 1.0),
            ("firstStopDepth", "stop_depth_m", 1.0),
            ("fractionO2", "o2_pct", 100.0),
            ("fractionHe", "he_pct", 100.0),
            ("averagePPO2", "avg_ppo2", 1.0),
            ("waterTemp", "temp_c", 1.0),
            ("batteryVoltage", "battery_v", 1.0),
        ]
        for i, (db_row, x) in enumerate(zip(samples, xs)):
            for xml_field, col, scale in pairs:
                xv = x.get(xml_field)
                dv = db_row[col]
                if xv is None or dv is None:
                    continue
                delta = abs(xv * scale - dv)
                if delta > TOL[col]:
                    _fail(
                        failures,
                        f"dive #{number} sample {i} {col}: pnf={dv} xml={xv * scale}",
                    )
                    break
                if delta > worst:
                    worst, worst_field = delta, f"#{number}[{i}].{col}"
            else:
                continue
            break
        checked += 1
    print(f"xml crosscheck: {checked} dives, worst deviation {worst:.4f} at {worst_field}")


def check_gf99(con: sqlite3.Connection, failures: list) -> None:
    rows = con.execute(
        "SELECT id, header_json FROM source_dives WHERE source='shearwater'"
    ).fetchall()
    checked = 0
    for row in rows:
        header = json.loads(row["header_json"])
        end_gf = header.get("log_data", {}).get(
            "calculated_values_from_samples", {}
        ).get("EndGF99")
        if end_gf is None or end_gf < 0:
            continue
        tail = con.execute(
            "SELECT max(gf99) FROM (SELECT gf99 FROM shearwater_samples"
            " WHERE source_dive_id=? AND gf99 IS NOT NULL ORDER BY t_s DESC LIMIT 60)",
            (row["id"],),
        ).fetchone()[0]
        if tail is None:
            continue
        if abs(tail - end_gf) > 1.0:
            _fail(failures, f"source dive {row['id']}: gf99 tail {tail} vs EndGF99 {end_gf}")
        checked += 1
    viol = con.execute(
        "SELECT count(*) FROM shearwater_samples"
        " WHERE stop_depth_m > 0 AND ceiling_m IS NOT NULL AND ceiling_m > stop_depth_m + 0.01"
    ).fetchone()[0]
    if viol:
        _fail(failures, f"{viol} samples with ceiling above stop depth")
    print(f"gf99/ceiling: {checked} dives checked against EndGF99")


def check_twins(con: sqlite3.Connection, failures: list) -> None:
    matches = con.execute(
        "SELECT m.garmin_source_dive_id AS g, m.shearwater_source_dive_id AS s,"
        " m.clock_offset_s, m.residual_skew_s, m.xcorr_score"
        " FROM matches m WHERE m.status IN ('auto','confirmed')"
    ).fetchall()
    medians = []
    for m in matches:
        g_rows = con.execute(
            "SELECT t_s, depth_m FROM garmin_samples WHERE source_dive_id=?"
            " AND depth_m IS NOT NULL ORDER BY t_s",
            (m["g"],),
        ).fetchall()
        s_rows = con.execute(
            "SELECT t_s, depth_m FROM shearwater_samples WHERE source_dive_id=?"
            " AND depth_m IS NOT NULL ORDER BY t_s",
            (m["s"],),
        ).fetchall()
        g_start = con.execute(
            "SELECT start_time_utc FROM source_dives WHERE id=?", (m["g"],)
        ).fetchone()[0]
        s_start = con.execute(
            "SELECT start_time_utc FROM source_dives WHERE id=?", (m["s"],)
        ).fetchone()[0]
        if not g_rows or not s_rows or s_start is None:
            continue
        fmt = "%Y-%m-%d %H:%M:%S"
        base_delta = (
            datetime.strptime(s_start, fmt) - datetime.strptime(g_start, fmt)
        ).total_seconds() + (m["residual_skew_s"] or 0)

        g_times = [r[0] for r in g_rows]
        g_depths = [r[1] for r in g_rows]
        deltas = []
        for t_s, depth in s_rows:
            target = t_s + base_delta
            i = bisect.bisect_left(g_times, target)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(g_times):
                    d = abs(g_times[j] - target)
                    if best is None or d < best[0]:
                        best = (d, g_depths[j])
            if best and best[0] <= 6:
                deltas.append(abs(best[1] - depth))
        if not deltas:
            continue
        deltas.sort()
        median = deltas[len(deltas) // 2]
        medians.append(median)
        # The two computers convert pressure to depth with their own water
        # density settings (salt/EN13319/fresh differ by up to ~3%), so the
        # tolerance scales with depth.
        max_depth = max((d for _, d in s_rows), default=0)
        if median > max(0.6, 0.045 * max_depth):
            _fail(
                failures,
                f"match g={m['g']} s={m['s']}: median depth delta {median:.2f} m"
                f" at max depth {max_depth:.1f} m (score {m['xcorr_score']})",
            )
    if medians:
        print(
            f"twin agreement: {len(medians)} matches,"
            f" median-of-medians {sorted(medians)[len(medians) // 2]:.3f} m,"
            f" worst {max(medians):.3f} m"
        )


def check_counts(con: sqlite3.Connection, failures: list) -> None:
    counts = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT source, count(*) FROM source_dives GROUP BY source"
        )
    }
    print(f"source dives: {counts}")
    orphans = con.execute(
        "SELECT count(*) FROM source_dives sd WHERE NOT EXISTS"
        " (SELECT 1 FROM dive_members dm WHERE dm.source_dive_id = sd.id)"
    ).fetchone()[0]
    if orphans:
        _fail(failures, f"{orphans} source dives not in any canonical dive")
    n_matches = con.execute(
        "SELECT count(*) FROM matches WHERE status IN ('auto','confirmed')"
    ).fetchone()[0]
    n_dives = con.execute("SELECT count(*) FROM dives").fetchone()[0]
    n_undecoded = con.execute("SELECT count(*) FROM undecoded_payloads").fetchone()[0]
    print(f"matches: {n_matches}, canonical dives: {n_dives}, undecoded payloads: {n_undecoded}")


def run_verify(con: sqlite3.Connection, xml_dir: Path | None = None) -> int:
    failures: list[str] = []
    check_counts(con, failures)
    check_gf99(con, failures)
    if xml_dir:
        check_xml(con, xml_dir, failures)
    check_twins(con, failures)
    if failures:
        print(f"\nverify: {len(failures)} failure(s)")
        return 1
    print("\nverify: all checks passed")
    return 0
