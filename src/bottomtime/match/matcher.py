"""Match Shearwater dives to Garmin dives and build canonical dive rows.

A Shearwater log's clock is a local wall clock with no zone; Garmin supplies
true UTC plus the local offset. For each Shearwater dive we try the UTC
offsets observed on Garmin dives nearby in calendar time, look for interval
overlap, and confirm with depth-profile cross-correlation computed over the
overlapping window.

Two realities this must handle (observed in real data):
- The two computers segment differently: a Garmin activity may span several
  Shearwater logs (pool sessions, drills), so one Garmin dive may match many
  Shearwater dives as long as their intervals don't collide.
- Recorded durations differ systematically (the Descent keeps logging through
  surface pauses), so duration agreement is evidence, never a hard gate.

Matching is non-destructive: both sample series are kept; links (with clock
offset and residual skew) live in `matches`, and canonical dives are the
connected components of those links.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..store.db import utcnow
from .correlate import best_lag, median_abs_delta

TEST_MAX_DURATION_S = 120
TEST_MAX_DEPTH_M = 2.0
MIN_OVERLAP_FRACTION = 0.5   # of the shorter log
XCORR_ACCEPT = 0.95
# Second acceptance path for low-variance (pool) profiles where NCC is
# unstable: near-total time overlap plus same-body depth agreement.
DELTA_OVERLAP_MIN = 0.85
DELTA_ACCEPT_M = 0.75
AMBIGUITY_MARGIN = 0.02
DEFAULT_OFFSET_MIN = 480     # fallback when no Garmin dive is near in time


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _is_test(duration_s, max_depth_m) -> bool:
    return (duration_s or 0) < TEST_MAX_DURATION_S or (max_depth_m or 0) < TEST_MAX_DEPTH_M


def _depth_series(con: sqlite3.Connection, table: str, dive_id: int, t_base: float):
    rows = con.execute(
        f"SELECT t_s, depth_m FROM {table} WHERE source_dive_id=? AND depth_m IS NOT NULL ORDER BY t_s",
        (dive_id,),
    ).fetchall()
    return [(t_base + r[0], r[1]) for r in rows]


def _overlap_fraction(a_start, a_dur, b_start, b_dur) -> float:
    a_end = a_start + timedelta(seconds=a_dur or 0)
    b_end = b_start + timedelta(seconds=b_dur or 0)
    overlap = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    shorter = max(min(a_dur or 0, b_dur or 0), 1.0)
    return max(overlap, 0.0) / shorter


def run_match(con: sqlite3.Connection, verbose: bool = True) -> dict:
    garmin = [
        dict(r)
        for r in con.execute(
            "SELECT id, start_time_utc, start_time_local, utc_offset_min, duration_s,"
            " max_depth_m FROM source_dives WHERE source='garmin'"
        )
    ]
    shearwater = [
        dict(r)
        for r in con.execute(
            "SELECT id, start_time_local, duration_s, max_depth_m"
            " FROM source_dives WHERE source='shearwater'"
        )
    ]

    for g in garmin:
        g["start_utc_dt"] = _dt(g["start_time_utc"])
        g["start_local_dt"] = _dt(g["start_time_local"])
        g["test"] = _is_test(g["duration_s"], g["max_depth_m"])
    for s in shearwater:
        s["wall_dt"] = _dt(s["start_time_local"])
        s["test"] = _is_test(s["duration_s"], s["max_depth_m"])

    candidates: dict[int, list[dict]] = {}
    for s in shearwater:
        if s["test"]:
            continue
        offsets = {
            g["utc_offset_min"]
            for g in garmin
            if g["utc_offset_min"] is not None
            and abs((g["start_local_dt"] - s["wall_dt"]).total_seconds()) < 2 * 86400
        }
        offsets.add(DEFAULT_OFFSET_MIN)
        found = []
        for offset in offsets:
            s_utc = s["wall_dt"] - timedelta(minutes=offset)
            for g in garmin:
                if g["test"]:
                    continue
                frac = _overlap_fraction(
                    g["start_utc_dt"], g["duration_s"], s_utc, s["duration_s"]
                )
                if frac < MIN_OVERLAP_FRACTION:
                    continue
                # The shorter log cannot be meaningfully deeper than the longer.
                if (s["duration_s"] or 0) <= (g["duration_s"] or 0):
                    shallower, deeper = s["max_depth_m"], g["max_depth_m"]
                else:
                    shallower, deeper = g["max_depth_m"], s["max_depth_m"]
                if (shallower or 0) > (deeper or 0) * 1.05 + 1.0:
                    continue

                g_series = _depth_series(
                    con, "garmin_samples", g["id"], g["start_utc_dt"].timestamp()
                )
                s_series = _depth_series(
                    con, "shearwater_samples", s["id"], s_utc.timestamp()
                )
                lag, score = best_lag(g_series, s_series)
                accepted_by = None
                if score >= XCORR_ACCEPT:
                    accepted_by = "profile"
                elif frac >= DELTA_OVERLAP_MIN:
                    delta = median_abs_delta(g_series, s_series, lag)
                    if delta is not None and delta <= DELTA_ACCEPT_M:
                        accepted_by = f"depth-delta={delta:.2f}"
                if accepted_by:
                    found.append(
                        {
                            "garmin": g,
                            "offset_min": offset,
                            "lag_s": lag,
                            "score": score,
                            "accepted_by": accepted_by,
                            "overlap": frac,
                            "dur_delta": abs(
                                (g["duration_s"] or 0) - (s["duration_s"] or 0)
                            ),
                            "depth_delta": abs(
                                (g["max_depth_m"] or 0) - (s["max_depth_m"] or 0)
                            ),
                            "s_utc": s_utc,
                        }
                    )
        candidates[s["id"]] = sorted(found, key=lambda c: -c["score"])

    rejected = {
        (r[0], r[1])
        for r in con.execute(
            "SELECT garmin_source_dive_id, shearwater_source_dive_id FROM matches"
            " WHERE status='rejected'"
        )
    }
    confirmed_s = {
        r[0]
        for r in con.execute(
            "SELECT shearwater_source_dive_id FROM matches WHERE status='confirmed'"
        )
    }

    # Greedy by score. A Garmin dive may take several Shearwater partners as
    # long as their (offset-corrected) intervals don't collide.
    all_pairs = []
    for s_id, cands in candidates.items():
        for c in cands:
            all_pairs.append((c["score"], s_id, c))
    all_pairs.sort(key=lambda x: -x[0])

    s_by_id = {s["id"]: s for s in shearwater}
    taken_s = set(confirmed_s)
    garmin_partners: dict[int, list[tuple[datetime, datetime]]] = {}
    assignments: dict[int, dict] = {}
    ambiguous: list[int] = []

    for score, s_id, c in all_pairs:
        if s_id in taken_s:
            continue
        g_id = c["garmin"]["id"]
        if (g_id, s_id) in rejected:
            continue
        cands = candidates[s_id]
        if (
            len(cands) > 1
            and cands[0]["score"] - cands[1]["score"] < AMBIGUITY_MARGIN
            and cands[0]["garmin"]["id"] != cands[1]["garmin"]["id"]
            and s_id not in assignments
        ):
            ambiguous.append(s_id)
            taken_s.add(s_id)
            continue
        interval = (
            c["s_utc"],
            c["s_utc"] + timedelta(seconds=s_by_id[s_id]["duration_s"] or 0),
        )
        collision = any(
            not (interval[1] <= lo or interval[0] >= hi)
            for lo, hi in garmin_partners.get(g_id, [])
        )
        if collision:
            continue
        assignments[s_id] = c
        taken_s.add(s_id)
        garmin_partners.setdefault(g_id, []).append(interval)

    with con:
        con.execute("DELETE FROM matches WHERE status='auto'")
        for s_id, c in assignments.items():
            con.execute(
                "INSERT INTO matches (garmin_source_dive_id, shearwater_source_dive_id,"
                " clock_offset_s, residual_skew_s, xcorr_score, duration_delta_s,"
                " max_depth_delta_m, method, status, matched_at)"
                " VALUES (?,?,?,?,?,?,?,?,'auto',?)",
                (
                    c["garmin"]["id"],
                    s_id,
                    c["offset_min"] * 60,
                    c["lag_s"],
                    round(c["score"], 5),
                    c["dur_delta"],
                    c["depth_delta"],
                    f"overlap={c['overlap']:.2f}+{c['accepted_by']}",
                    utcnow(),
                ),
            )
        _rebuild_dives(con, garmin, shearwater)
        _backfill_shearwater_utc(con)

    stats = {
        "matched": len(assignments),
        "ambiguous": len(ambiguous),
        "shearwater_unmatched": sum(
            1
            for s in shearwater
            if not s["test"] and s["id"] not in taken_s
        ),
    }
    if verbose and ambiguous:
        print(f"ambiguous shearwater dives needing review: {ambiguous}")
    return stats


def _rebuild_dives(con, garmin, shearwater):
    """Rebuild canonical dives as connected components of match links."""
    con.execute("DELETE FROM dive_members")
    con.execute("DELETE FROM dives")

    matches = con.execute(
        "SELECT garmin_source_dive_id AS g, shearwater_source_dive_id AS s,"
        " clock_offset_s FROM matches WHERE status IN ('auto','confirmed')"
    ).fetchall()
    g_by_id = {g["id"]: g for g in garmin}
    s_by_id = {s["id"]: s for s in shearwater}

    partners: dict[int, list] = {}
    matched_s = {}
    for m in matches:
        partners.setdefault(m["g"], []).append(m)
        matched_s[m["s"]] = m

    entries = []
    for g in garmin:
        members = [g["id"]] + [m["s"] for m in partners.get(g["id"], [])]
        entries.append(
            {
                "start_utc": g["start_time_utc"],
                "offset_min": g["utc_offset_min"],
                "duration": g["duration_s"],
                "depth": max(
                    [g["max_depth_m"] or 0]
                    + [s_by_id[m["s"]]["max_depth_m"] or 0 for m in partners.get(g["id"], [])]
                ),
                "test": g["test"],
                "members": members,
            }
        )
    default = timedelta(minutes=DEFAULT_OFFSET_MIN)
    for s in shearwater:
        if s["id"] in matched_s:
            continue
        entries.append(
            {
                "start_utc": (s["wall_dt"] - default).strftime("%Y-%m-%d %H:%M:%S"),
                "offset_min": DEFAULT_OFFSET_MIN,
                "duration": s["duration_s"],
                "depth": s["max_depth_m"],
                "test": s["test"],
                "members": [s["id"]],
            }
        )

    entries.sort(key=lambda e: e["start_utc"])
    number = 0
    for e in entries:
        if not e["test"]:
            number += 1
        cur = con.execute(
            "INSERT INTO dives (dive_number, start_time_utc, utc_offset_min,"
            " duration_s, max_depth_m, mode, is_test) VALUES (?,?,?,?,?,NULL,?)",
            (
                number if not e["test"] else None,
                e["start_utc"],
                e["offset_min"],
                e["duration"],
                e["depth"],
                1 if e["test"] else 0,
            ),
        )
        for member in e["members"]:
            con.execute(
                "INSERT INTO dive_members (dive_id, source_dive_id) VALUES (?,?)",
                (cur.lastrowid, member),
            )


def _backfill_shearwater_utc(con):
    """Set start_time_utc/utc_offset_min on matched Shearwater source dives."""
    for m in con.execute(
        "SELECT shearwater_source_dive_id AS s, clock_offset_s FROM matches"
        " WHERE status IN ('auto','confirmed')"
    ).fetchall():
        row = con.execute(
            "SELECT start_time_local FROM source_dives WHERE id=?", (m["s"],)
        ).fetchone()
        utc = _dt(row[0]) - timedelta(seconds=m["clock_offset_s"])
        con.execute(
            "UPDATE source_dives SET start_time_utc=?, utc_offset_min=? WHERE id=?",
            (utc.strftime("%Y-%m-%d %H:%M:%S"), m["clock_offset_s"] // 60, m["s"]),
        )
