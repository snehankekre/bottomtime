"""Small read API for analysis code.

    import bottomtime
    dive = bottomtime.load_dive("data/dives.db", 291)
    dive["sources"]["garmin"][0]["samples"]["depth_m"]
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from .store.schema import SOURCES


def computer_info(source: str, header: dict) -> dict:
    """Model / firmware / serial of the computer that produced a source log,
    extracted from its verbatim header."""
    if source == "garmin":
        file_id = (header.get("file_id_mesgs") or [{}])[0]
        product = file_id.get("garmin_product") or file_id.get("product_name")
        model = str(product).replace("_", " ").title() if product else None
        if product == "connect":
            model = "Entered by hand in Garmin Connect"
        firmware = next(
            (
                d.get("software_version")
                for d in header.get("device_info_mesgs") or []
                if d.get("software_version")
            ),
            None,
        )
        serial = file_id.get("serial_number")
        return {"model": model, "firmware": firmware, "serial": serial}

    pnf_header = header.get("pnf") or {}
    firmware = pnf_header.get("computer_firmware")
    serial = pnf_header.get("computer_serial")
    return {
        "model": pnf_header.get("computer_model"),
        "firmware": f"v{firmware}" if firmware is not None else None,
        "serial": f"{serial:08X}" if serial is not None else None,
    }


def _columns_of(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def load_dive(
    db_path: str | Path, dive_number: int | None = None, *, dive_id: int | None = None
) -> dict:
    """Load one canonical dive with every member source log's samples as
    column-oriented lists (ready for plotting / DataFrame construction).

    ``dive["sources"]`` maps each source to a list of its logs, because one
    Garmin activity can match several Shearwater logs (a pool session)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        if dive_id is not None:
            dive = con.execute("SELECT * FROM dives WHERE id=?", (dive_id,)).fetchone()
        else:
            dive = con.execute(
                "SELECT * FROM dives WHERE dive_number=?", (dive_number,)
            ).fetchone()
        if dive is None:
            raise KeyError(f"no dive with number {dive_number}")
        out = {k: dive[k] for k in dive.keys()}
        out["sources"] = {}
        members = con.execute(
            "SELECT sd.*, dm.offset_s FROM source_dives sd JOIN dive_members dm"
            " ON dm.source_dive_id = sd.id WHERE dm.dive_id=?"
            " ORDER BY dm.offset_s",
            (dive["id"],),
        ).fetchall()
        for sd in members:
            table = SOURCES[sd["source"]]["samples"]
            cols = [c for c in _columns_of(con, table) if c != "source_dive_id"]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM {table}"
                " WHERE source_dive_id=? ORDER BY t_s",
                (sd["id"],),
            ).fetchall()
            samples = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
            header = json.loads(sd["header_json"])
            entry = {
                "source_dive_id": sd["id"],
                "source_key": sd["source_key"],
                "device": sd["device"],
                # Seconds from the dive's start to this log's t_s = 0.
                "offset_s": sd["offset_s"],
                "start_time_utc": sd["start_time_utc"],
                "start_time_local": sd["start_time_local"],
                "duration_s": sd["duration_s"],
                "max_depth_m": sd["max_depth_m"],
                "sample_interval_ms": sd["sample_interval_ms"],
                "mode": sd["mode"],
                "computer": computer_info(sd["source"], header),
                "header": header,
                "samples": samples,
                "gases": [
                    {k: g[k] for k in g.keys()}
                    for g in con.execute(
                        "SELECT gas_index, o2_pct, he_pct, circuit, enabled"
                        " FROM gases WHERE source_dive_id=? ORDER BY gas_index",
                        (sd["id"],),
                    )
                ],
                "events": [
                    {"t_s": e["t_s"], "kind": e["kind"],
                     "payload": json.loads(e["payload_json"])}
                    for e in con.execute(
                        "SELECT t_s, kind, payload_json FROM events"
                        " WHERE source_dive_id=? ORDER BY t_s",
                        (sd["id"],),
                    )
                ],
            }
            out["sources"].setdefault(sd["source"], []).append(entry)
        out["matches"] = [
            {k: m[k] for k in m.keys()}
            for m in con.execute(
                "SELECT m.a_source_dive_id, m.b_source_dive_id,"
                " m.clock_offset_s, m.residual_skew_s, m.xcorr_score,"
                " m.duration_delta_s, m.max_depth_delta_m, m.method, m.status"
                " FROM matches m JOIN dive_members dm"
                " ON dm.source_dive_id = m.a_source_dive_id"
                " WHERE dm.dive_id=? AND m.status IN ('auto','confirmed')",
                (dive["id"],),
            )
        ]
        return out
    finally:
        con.close()


def source_logs(dive: dict):
    """Yield (source, log) for every member log of a loaded dive."""
    for source, logs in dive["sources"].items():
        for log in logs:
            yield source, log


def write_dive_csv(dive: dict, fh: TextIO) -> None:
    """Every sample of every log of a loaded dive, one row per sample, never
    merged. dive_time_s puts all the logs on the dive's shared timeline."""
    columns: list[str] = []
    for _source, log in source_logs(dive):
        for c in log["samples"]:
            if c not in columns:
                columns.append(c)
    writer = csv.writer(fh)
    writer.writerow(["source", "source_dive_id", "computer", "dive_time_s"] + columns)
    for source, log in source_logs(dive):
        samples = log["samples"]
        for i, t in enumerate(samples.get("t_s", [])):
            writer.writerow(
                [source, log["source_dive_id"], log["computer"]["model"],
                 round(log["offset_s"] + t, 1)]
                + [samples[c][i] if c in samples else None for c in columns]
            )


def dive_summaries(db_path: str | Path) -> list[dict]:
    """One row per canonical dive with the numbers a dive list sorts on."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        per_log: dict[int, dict] = {}
        # Surfacing GF99 is the highest value in the last 60 samples (ten
        # minutes), the window verify uses against Cloud's EndGF99.
        for r in con.execute(
            "SELECT s.source_dive_id AS id, max(s.gf99) AS gf99_max,"
            " max(s.cns_pct) AS cns, sum(s.in_deco) * max(sd.sample_interval_ms) / 60000.0 AS deco_min,"
            " (SELECT max(gf99) FROM (SELECT gf99 FROM shearwater_samples x"
            "   WHERE x.source_dive_id = s.source_dive_id ORDER BY t_s DESC LIMIT 60)) AS gf99_surf"
            " FROM shearwater_samples s JOIN source_dives sd ON sd.id = s.source_dive_id"
            " GROUP BY s.source_dive_id"
        ):
            per_log[r["id"]] = dict(r)
        for r in con.execute(
            "SELECT g.source_dive_id AS id, max(g.cns_pct) AS cns,"
            " sum(g.next_stop_depth_m > 0) * max(sd.sample_interval_ms) / 60000.0 AS deco_min"
            " FROM garmin_samples g JOIN source_dives sd ON sd.id = g.source_dive_id"
            " GROUP BY g.source_dive_id"
        ):
            per_log[r["id"]] = dict(r)

        members: dict[int, list] = defaultdict(list)
        for r in con.execute(
            "SELECT dm.dive_id, sd.id, sd.source, sd.model, sd.mode"
            " FROM dive_members dm JOIN source_dives sd ON sd.id = dm.source_dive_id"
            " ORDER BY dm.offset_s"
        ):
            members[r["dive_id"]].append(r)

        def best(logs, key):
            values = [per_log.get(m["id"], {}).get(key) for m in logs]
            values = [v for v in values if v is not None]
            return max(values) if values else None

        out = []
        for d in con.execute("SELECT * FROM dives ORDER BY start_time_utc"):
            logs = members[d["id"]]
            out.append(
                {
                    "number": d["dive_number"],
                    "id": d["id"],
                    "start_time_utc": d["start_time_utc"],
                    "utc_offset_min": d["utc_offset_min"],
                    "duration_s": d["duration_s"],
                    "max_depth_m": d["max_depth_m"],
                    "is_test": bool(d["is_test"]),
                    "computers": [m["model"] or m["source"] for m in logs],
                    "mode": next((m["mode"] for m in logs if m["source"] == "shearwater"), None)
                    or next((m["mode"] for m in logs), None),
                    "gf99_max": best(logs, "gf99_max"),
                    "gf99_surf": best(logs, "gf99_surf"),
                    "cns_pct": best(logs, "cns"),
                    "deco_min": best(logs, "deco_min"),
                }
            )
        return out
    finally:
        con.close()


def list_dives(db_path: str | Path, include_tests: bool = False) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        where = "" if include_tests else "WHERE d.is_test = 0"
        return [
            {k: r[k] for k in r.keys()}
            for r in con.execute(
                "SELECT d.*, group_concat(sd.source) AS sources FROM dives d"
                " JOIN dive_members dm ON dm.dive_id = d.id"
                " JOIN source_dives sd ON sd.id = dm.source_dive_id"
                f" {where} GROUP BY d.id ORDER BY d.start_time_utc"
            )
        ]
    finally:
        con.close()
