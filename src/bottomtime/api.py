"""Small read API for analysis code.

    import bottomtime
    dive = bottomtime.load_dive("data/dives.db", 291)
    dive["sources"]["garmin"]["samples"]["depth_m"]
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SAMPLE_TABLES = {"garmin": "garmin_samples", "shearwater": "shearwater_samples"}


def _columns_of(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def load_dive(db_path: str | Path, dive_number: int) -> dict:
    """Load one canonical dive with every member source log's samples as
    column-oriented lists (ready for plotting / DataFrame construction)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        dive = con.execute(
            "SELECT * FROM dives WHERE dive_number=?", (dive_number,)
        ).fetchone()
        if dive is None:
            raise KeyError(f"no dive with number {dive_number}")
        out = {k: dive[k] for k in dive.keys()}
        out["sources"] = {}
        members = con.execute(
            "SELECT sd.* FROM source_dives sd JOIN dive_members dm"
            " ON dm.source_dive_id = sd.id WHERE dm.dive_id=?",
            (dive["id"],),
        ).fetchall()
        for sd in members:
            table = _SAMPLE_TABLES[sd["source"]]
            cols = [c for c in _columns_of(con, table) if c != "source_dive_id"]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM {table}"
                " WHERE source_dive_id=? ORDER BY t_s",
                (sd["id"],),
            ).fetchall()
            samples = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
            entry = {
                "source_dive_id": sd["id"],
                "source_key": sd["source_key"],
                "start_time_utc": sd["start_time_utc"],
                "start_time_local": sd["start_time_local"],
                "duration_s": sd["duration_s"],
                "max_depth_m": sd["max_depth_m"],
                "mode": sd["mode"],
                "header": json.loads(sd["header_json"]),
                "samples": samples,
            }
            key = sd["source"]
            if key in out["sources"]:  # several logs from one source (splits)
                existing = out["sources"][key]
                if isinstance(existing, list):
                    existing.append(entry)
                else:
                    out["sources"][key] = [existing, entry]
            else:
                out["sources"][key] = entry
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
