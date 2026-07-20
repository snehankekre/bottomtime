"""Small read API for analysis code.

    import bottomtime
    dive = bottomtime.load_dive("data/dives.db", 291)
    dive["sources"]["garmin"]["samples"]["depth_m"]
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .decode.pnf import COMPUTER_MODELS

_SAMPLE_TABLES = {"garmin": "garmin_samples", "shearwater": "shearwater_samples"}


def computer_info(source: str, header: dict) -> dict:
    """Model / firmware / serial of the computer that produced a source log,
    extracted from its verbatim header."""
    if source == "garmin":
        file_id = (header.get("file_id_mesgs") or [{}])[0]
        product = file_id.get("garmin_product") or file_id.get("product_name")
        model = str(product).replace("_", " ").title() if product else None
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
    model_id = pnf_header.get("computer_model")
    model = (
        COMPUTER_MODELS.get(model_id, f"model {model_id}")
        if model_id is not None
        else None
    )
    firmware = pnf_header.get("computer_firmware")
    serial = (header.get("dive_details") or {}).get("SerialNumber")
    if not serial and pnf_header.get("computer_serial") is not None:
        serial = f"{pnf_header['computer_serial']:08X}"
    return {
        "model": model,
        "firmware": f"v{firmware}" if firmware is not None else None,
        "serial": serial,
    }


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
            header = json.loads(sd["header_json"])
            entry = {
                "source_dive_id": sd["id"],
                "source_key": sd["source_key"],
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
                        "SELECT gas_index, o2_pct, he_pct, circuit, enabled, used"
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
            key = sd["source"]
            if key in out["sources"]:  # several logs from one source (splits)
                existing = out["sources"][key]
                if isinstance(existing, list):
                    existing.append(entry)
                else:
                    out["sources"][key] = [existing, entry]
            else:
                out["sources"][key] = entry
        out["matches"] = [
            {k: m[k] for k in m.keys()}
            for m in con.execute(
                "SELECT m.garmin_source_dive_id, m.shearwater_source_dive_id,"
                " m.clock_offset_s, m.residual_skew_s, m.xcorr_score,"
                " m.duration_delta_s, m.max_depth_delta_m, m.method, m.status"
                " FROM matches m JOIN dive_members dm"
                " ON dm.source_dive_id = m.garmin_source_dive_id"
                " WHERE dm.dive_id=? AND m.status IN ('auto','confirmed')",
                (dive["id"],),
            )
        ]
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
