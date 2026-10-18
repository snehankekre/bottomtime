"""Match logs of the same dive across computers and build canonical dives.

Each source kind declares its clock in store.schema.SOURCES. Garmin logs carry
true UTC; Shearwater logs carry a wall clock with no zone. Two logs from
different devices are compared on a shared clock: both on UTC, both on the
wall clock, or a wall-clock log shifted by each UTC offset that UTC logs
recorded nearby. Interval overlap proposes a pair and depth-profile
cross-correlation accepts it. Flat pool profiles, where correlation is noise,
are accepted instead on near-total overlap plus same-body depth agreement.

Two realities this must handle (observed in real data):
- Computers segment differently: a Garmin activity may span several
  Shearwater logs (pool sessions, drills), so one log may match several logs
  from another device as long as those don't collide in time.
- Recorded durations differ systematically (the Descent keeps logging through
  surface pauses), so duration agreement is never evidence.

In each pair, `a` is the log the other is lined up against: the UTC log when
only one has UTC, otherwise the longer one. `b` takes at most one partner per
device of `a`; `a` may take several from one device if they don't overlap.

Matching is non-destructive: every sample series stays as recorded. Links
(clock offset and residual skew) live in `matches`; canonical dives are the
connected components of those links, and each member's offset from the dive's
start is stored on dive_members, so a display can line the logs up without
resampling either.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from ..store.db import utcnow
from ..store.schema import SOURCES
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
NEAR = timedelta(days=2)
FMT = "%Y-%m-%d %H:%M:%S"


def _dt(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def _is_test(duration_s, max_depth_m) -> bool:
    return (duration_s or 0) < TEST_MAX_DURATION_S or (max_depth_m or 0) < TEST_MAX_DEPTH_M


def _overlap_fraction(a_start, a_dur, b_start, b_dur) -> float:
    a_end = a_start + timedelta(seconds=a_dur or 0)
    b_end = b_start + timedelta(seconds=b_dur or 0)
    overlap = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    shorter = max(min(a_dur or 0, b_dur or 0), 1.0)
    return max(overlap, 0.0) / shorter


def _own_start(log: dict) -> datetime:
    return log["utc"] if log["utc_clock"] else log["wall"]


def run_match(con: sqlite3.Connection, verbose: bool = True) -> dict:
    logs = [
        dict(r)
        for r in con.execute(
            "SELECT id, source, device, start_time_utc, start_time_local,"
            " utc_offset_min, duration_s, max_depth_m FROM source_dives"
            " ORDER BY start_time_local, id"
        )
    ]
    for d in logs:
        d["utc_clock"] = SOURCES[d["source"]]["clock"] == "utc"
        d["wall"] = _dt(d["start_time_local"])
        d["utc"] = _dt(d["start_time_utc"]) if d["utc_clock"] else None
        d["test"] = _is_test(d["duration_s"], d["max_depth_m"])

    # Fallback offset for a wall-clock log with no UTC log near it in time: the
    # offset the diver's UTC logs recorded most often, else UTC itself.
    seen = Counter(
        d["utc_offset_min"] for d in logs if d["utc_clock"] and d["utc_offset_min"] is not None
    )
    default_offset = seen.most_common(1)[0][0] if seen else 0

    def offsets_near(f: dict) -> set[int]:
        found = {
            d["utc_offset_min"]
            for d in logs
            if d["utc_clock"]
            and d["utc_offset_min"] is not None
            and abs(d["wall"] - f["wall"]) < NEAR
        }
        return found | {default_offset}

    depths: dict[int, list] = {}

    def series(log: dict, start: datetime) -> list[tuple[float, float]]:
        if log["id"] not in depths:
            table = SOURCES[log["source"]]["samples"]
            depths[log["id"]] = con.execute(
                f"SELECT t_s, depth_m FROM {table}"
                " WHERE source_dive_id=? AND depth_m IS NOT NULL ORDER BY t_s",
                (log["id"],),
            ).fetchall()
        base = start.replace(tzinfo=timezone.utc).timestamp()
        return [(base + t, v) for t, v in depths[log["id"]]]

    real = [d for d in logs if not d["test"]]
    found = []
    for i, x in enumerate(real):
        for y in real[i + 1 :]:
            if y["wall"] - x["wall"] >= NEAR:
                break
            if x["device"] == y["device"]:
                continue
            if x["utc_clock"] != y["utc_clock"]:
                a, b = (x, y) if x["utc_clock"] else (y, x)
                options = [
                    (off * 60, a["utc"], b["wall"] - timedelta(minutes=off))
                    for off in offsets_near(b)
                ]
            else:
                a, b = (x, y) if (x["duration_s"] or 0) >= (y["duration_s"] or 0) else (y, x)
                options = [(0, _own_start(a), _own_start(b))]
            for offset_s, a_start, b_start in options:
                c = _evaluate(a, b, a_start, b_start, series)
                if c:
                    c["offset_s"] = offset_s
                    found.append(c)

    rejected = {
        (r[0], r[1])
        for r in con.execute(
            "SELECT a_source_dive_id, b_source_dive_id FROM matches WHERE status='rejected'"
        )
    }
    by_id = {d["id"]: d for d in logs}
    taken = {
        (r[0], by_id[r[1]]["device"])
        for r in con.execute(
            "SELECT b_source_dive_id, a_source_dive_id FROM matches WHERE status='confirmed'"
        )
    }

    # Greedy by score. b takes one partner per device of a; a may take several
    # partners from one device as long as their intervals don't collide.
    by_b: dict[tuple, list] = defaultdict(list)
    for c in found:
        by_b[(c["b"]["id"], c["a"]["device"])].append(c)
    for cands in by_b.values():
        cands.sort(key=lambda c: -c["score"])

    partners: dict[tuple, list] = defaultdict(list)
    assignments: list[dict] = []
    ambiguous: list[int] = []
    for c in sorted(found, key=lambda c: -c["score"]):
        a, b = c["a"], c["b"]
        key = (b["id"], a["device"])
        if key in taken or (a["id"], b["id"]) in rejected:
            continue
        cands = by_b[key]
        if (
            len(cands) > 1
            and cands[0]["score"] - cands[1]["score"] < AMBIGUITY_MARGIN
            and cands[0]["a"]["id"] != cands[1]["a"]["id"]
        ):
            ambiguous.append(b["id"])
            taken.add(key)
            continue
        lo = c["b_start"]
        hi = lo + timedelta(seconds=b["duration_s"] or 0)
        if any(not (hi <= x or lo >= y) for x, y in partners[(a["id"], b["device"])]):
            continue
        assignments.append(c)
        taken.add(key)
        partners[(a["id"], b["device"])].append((lo, hi))

    with con:
        con.execute("DELETE FROM matches WHERE status='auto'")
        for c in assignments:
            con.execute(
                "INSERT INTO matches (a_source_dive_id, b_source_dive_id,"
                " clock_offset_s, residual_skew_s, xcorr_score, duration_delta_s,"
                " max_depth_delta_m, method, status, matched_at)"
                " VALUES (?,?,?,?,?,?,?,?,'auto',?)",
                (
                    c["a"]["id"],
                    c["b"]["id"],
                    c["offset_s"],
                    c["lag_s"],
                    round(c["score"], 5),
                    c["dur_delta"],
                    c["depth_delta"],
                    f"overlap={c['overlap']:.2f}+{c['accepted_by']}",
                    utcnow(),
                ),
            )
        _rebuild_dives(con, logs, default_offset)

    linked = {i for c in assignments for i in (c["a"]["id"], c["b"]["id"])}
    stats = {
        "matched": len(assignments),
        "ambiguous": len(ambiguous),
        "unmatched": sum(1 for d in real if d["id"] not in linked),
    }
    if verbose and ambiguous:
        print(f"ambiguous source dives needing review: {sorted(set(ambiguous))}")
    return stats


def _evaluate(a, b, a_start, b_start, series) -> dict | None:
    frac = _overlap_fraction(a_start, a["duration_s"], b_start, b["duration_s"])
    if frac < MIN_OVERLAP_FRACTION:
        return None
    # The shorter log cannot be meaningfully deeper than the longer.
    if (b["duration_s"] or 0) <= (a["duration_s"] or 0):
        shallower, deeper = b["max_depth_m"], a["max_depth_m"]
    else:
        shallower, deeper = a["max_depth_m"], b["max_depth_m"]
    if (shallower or 0) > (deeper or 0) * 1.05 + 1.0:
        return None

    a_series = series(a, a_start)
    b_series = series(b, b_start)
    lag, score = best_lag(a_series, b_series)
    accepted_by = None
    if score >= XCORR_ACCEPT:
        accepted_by = "profile"
    elif frac >= DELTA_OVERLAP_MIN:
        delta = median_abs_delta(a_series, b_series, lag)
        if delta is not None and delta <= DELTA_ACCEPT_M:
            accepted_by = f"depth-delta={delta:.2f}"
    if not accepted_by:
        return None
    return {
        "a": a,
        "b": b,
        "b_start": b_start,
        "lag_s": lag,
        "score": score,
        "accepted_by": accepted_by,
        "overlap": frac,
        "dur_delta": abs((a["duration_s"] or 0) - (b["duration_s"] or 0)),
        "depth_delta": abs((a["max_depth_m"] or 0) - (b["max_depth_m"] or 0)),
    }


def _rebuild_dives(con, logs, default_offset):
    """Rebuild canonical dives as connected components of match links."""
    con.execute("DELETE FROM dive_members")
    con.execute("DELETE FROM dives")
    local = tuple(s for s, spec in SOURCES.items() if spec["clock"] == "local")
    con.execute(
        f"UPDATE source_dives SET start_time_utc=NULL, utc_offset_min=NULL"
        f" WHERE source IN ({','.join('?' * len(local))})",
        local,
    )

    by_id = {d["id"]: d for d in logs}
    edges: dict[int, list] = defaultdict(list)
    for m in con.execute(
        "SELECT a_source_dive_id, b_source_dive_id, clock_offset_s, residual_skew_s"
        " FROM matches WHERE status IN ('auto','confirmed')"
    ):
        a, b, offset_s, skew = m
        a_log, b_log = by_id[a], by_id[b]
        # Where b starts relative to a, in seconds, once lined up.
        b_on_a = b_log["wall"] - timedelta(seconds=offset_s) if offset_s else _own_start(b_log)
        delta = (b_on_a - _own_start(a_log)).total_seconds() + (skew or 0)
        mixed = a_log["utc_clock"] != b_log["utc_clock"]
        edges[a].append((b, delta, offset_s if mixed else None))
        edges[b].append((a, -delta, offset_s if mixed else None))

    entries = []
    done: set[int] = set()
    for d in logs:
        if d["id"] in done:
            continue
        pos = {d["id"]: 0.0}
        zone = None  # whole-hour offset of the wall clocks, if a UTC log pins it
        stack = [d["id"]]
        while stack:
            u = stack.pop()
            for v, delta, offset_s in edges[u]:
                if offset_s is not None:
                    zone = offset_s
                if v not in pos:
                    pos[v] = pos[u] + delta
                    stack.append(v)
        done |= set(pos)
        members = [by_id[i] for i in pos]
        utc_members = [m for m in members if m["utc_clock"]]
        anchor = (
            min(utc_members, key=lambda m: m["utc"])
            if utc_members
            else max(members, key=lambda m: m["duration_s"] or 0)
        )
        if anchor["utc_clock"]:
            start_utc, offset_min = anchor["utc"], anchor["utc_offset_min"]
        else:
            offset_min = default_offset
            start_utc = anchor["wall"] - timedelta(minutes=offset_min)
        base = pos[anchor["id"]]
        entries.append(
            {
                "start_utc": start_utc.strftime(FMT),
                "offset_min": offset_min,
                "duration": anchor["duration_s"],
                "depth": max(m["max_depth_m"] or 0 for m in members),
                "test": anchor["test"],
                "members": [(m["id"], pos[m["id"]] - base) for m in members],
            }
        )
        if zone is not None:
            for m in members:
                if not m["utc_clock"]:
                    con.execute(
                        "UPDATE source_dives SET start_time_utc=?, utc_offset_min=? WHERE id=?",
                        (
                            (m["wall"] - timedelta(seconds=zone)).strftime(FMT),
                            zone // 60,
                            m["id"],
                        ),
                    )

    entries.sort(key=lambda e: e["start_utc"])
    number = 0
    for e in entries:
        if not e["test"]:
            number += 1
        cur = con.execute(
            "INSERT INTO dives (dive_number, start_time_utc, utc_offset_min,"
            " duration_s, max_depth_m, is_test) VALUES (?,?,?,?,?,?)",
            (
                number if not e["test"] else None,
                e["start_utc"],
                e["offset_min"],
                e["duration"],
                e["depth"],
                1 if e["test"] else 0,
            ),
        )
        con.executemany(
            "INSERT INTO dive_members (dive_id, source_dive_id, offset_s) VALUES (?,?,?)",
            [(cur.lastrowid, member, offset) for member, offset in e["members"]],
        )
