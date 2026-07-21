"""Ingest a Shearwater Cloud database (dive_data.db / 'Export Database').

Each dive's complete native log lives in log_data.data_bytes_1 as an sw-pnf
blob (decoded by the pnf package); dive_details supplies the metadata layer
(gas plan JSON, EndGF99, site/notes when present). The blob is stored verbatim
in source_dives.raw_blob in addition to the archived .db, so every Shearwater
byte is preserved twice over.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pnf

from ..store.db import source_dive_exists
from .archive import archive_file

# Decode-contract version stamped on each Shearwater source dive so that an
# archive-first re-decode can find dives produced by an older decode. Bump this
# whenever a pnf upgrade changes decoded output. History: 1 libdivecomputer
# field set, 2 empirical ceiling/gf99/battery/@+5, 3 pnf reconciliation
# (DCIEM branch, 16-bit battery, battery %, SAC, status bits).
DECODER_VERSION = 3


def _details_row(con: sqlite3.Connection, dive_id: str) -> dict:
    row = con.execute(
        "SELECT * FROM dive_details WHERE DiveId = ?", (dive_id,)
    ).fetchone()
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def ingest_shearwater_db(
    con: sqlite3.Connection, archive_dir: Path, db_path: Path
) -> dict:
    archive_file(con, archive_dir, "shearwater_db", db_path)

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    stats = {"ingested": 0, "skipped": 0, "failed": 0}
    rows = src.execute(
        "SELECT log_id, file_name, format, format_version,"
        " calculated_values_from_samples, data_bytes_1, data_bytes_2, data_bytes_3"
        " FROM log_data"
    ).fetchall()

    artifact_id = con.execute(
        "SELECT id FROM raw_artifacts WHERE kind='shearwater_db' AND original_path=?"
        " ORDER BY id DESC LIMIT 1",
        (str(db_path),),
    ).fetchone()[0]

    for row in rows:
        try:
            with con:
                result = _ingest_log(con, artifact_id, src, row)
            stats[result] += 1
        except Exception as e:
            stats["failed"] += 1
            print(f"FAILED {row['file_name']}: {type(e).__name__}: {e}")

    src.close()
    return stats


def _ingest_log(
    con: sqlite3.Connection,
    artifact_id: int,
    src: sqlite3.Connection,
    row: sqlite3.Row,
) -> str:
    if row["format"] != "sw-pnf":
        raise ValueError(f"unsupported log format {row['format']!r}")

    blob = row["data_bytes_1"]
    digest = hashlib.sha256(blob).hexdigest()
    if source_dive_exists(con, "shearwater", digest):
        return "skipped"

    dive = pnf.decode(blob)
    details = _details_row(src, row["log_id"])
    b2 = json.loads(row["data_bytes_2"]) if row["data_bytes_2"] else {}
    b3 = json.loads(row["data_bytes_3"]) if row["data_bytes_3"] else {}
    calc = (
        json.loads(row["calculated_values_from_samples"])
        if row["calculated_values_from_samples"]
        else {}
    )

    serial = details.get("SerialNumber")
    if not serial and "computer_serial" in dive.header:
        serial = f"{dive.header['computer_serial']:08X}"
    dive_number = details.get("DiveNumber") or b3.get("DiveNumber")
    source_key = f"{serial}#{dive_number}"

    # DIVE_START_TIME is the local wall clock stored as a fake-UTC epoch.
    start_epoch = b2.get("DIVE_START_TIME") or b3.get("StartTime")
    start_local = datetime.fromtimestamp(start_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    header = {
        "pnf": dive.header,
        "log_data": {
            "log_id": row["log_id"],
            "file_name": row["file_name"],
            "format": row["format"],
            "format_version": row["format_version"],
            "data_bytes_2": b2,
            "data_bytes_3": b3,
            "calculated_values_from_samples": calc,
        },
        "dive_details": details,
    }

    cur = con.execute(
        "INSERT INTO source_dives (source, artifact_id, source_key, content_hash,"
        " decoder_version, start_time_utc, start_time_local, utc_offset_min,"
        " duration_s, max_depth_m, avg_depth_m, sample_interval_ms, mode,"
        " header_json, raw_blob)"
        " VALUES ('shearwater',?,?,?,?,NULL,?,NULL,?,?,?,?,?,?,?)",
        (
            artifact_id,
            source_key,
            digest,
            DECODER_VERSION,
            start_local,
            dive.header.get("divetime_s"),
            dive.header.get("max_depth_m"),
            calc.get("AverageDepth"),
            dive.sample_interval_ms,
            dive.header.get("divemode"),
            json.dumps(header, separators=(",", ":")),
            blob,
        ),
    )
    dive_id = cur.lastrowid

    con.executemany(
        "INSERT INTO shearwater_samples (source_dive_id, t_s, depth_m, temp_c,"
        " stop_depth_m, stop_or_ndl_min, tts_min, in_deco, avg_ppo2, o2_pct, he_pct,"
        " setpoint, cns_pct, gf99, ceiling_m, safe_ascent_depth_m, battery_pct, sac,"
        " sensor1_raw, sensor2_raw, sensor3_raw,"
        " sensor1_ppo2, sensor2_ppo2, sensor3_ppo2, battery_v, status_flags,"
        " solenoid_fired_count, tank0_psi, tank1_psi, gas_time_min, extra_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                dive_id, s.t_s, s.depth_m, s.temp_c,
                s.stop_depth_m, s.stop_or_ndl_min, s.tts_min, s.in_deco,
                s.avg_ppo2, s.o2_pct, s.he_pct,
                s.setpoint, s.cns_pct, s.gf99, s.ceiling_m,
                s.safe_ascent_depth_m, s.battery_pct, s.sac,
                s.sensor1_raw, s.sensor2_raw, s.sensor3_raw,
                s.sensor1_ppo2, s.sensor2_ppo2, s.sensor3_ppo2,
                s.battery_v, s.status_flags,
                s.solenoid_fired_count,
                s.tank0_psi, s.tank1_psi, s.gas_time_min,
                json.dumps(s.extra, separators=(",", ":")) if s.extra else None,
            )
            for s in dive.samples
        ],
    )

    for gas in dive.gases:
        if gas.o2_pct == 0 and gas.he_pct == 0:
            continue
        con.execute(
            "INSERT INTO gases (source_dive_id, gas_index, o2_pct, he_pct, circuit,"
            " enabled, used, source_json) VALUES (?,?,?,?,?,?,NULL,NULL)",
            (
                dive_id,
                gas.index,
                gas.o2_pct,
                gas.he_pct,
                "cc" if gas.diluent else "oc",
                1 if gas.enabled else 0,
            ),
        )

    tank_profile = details.get("TankProfileData")
    if tank_profile:
        try:
            profile = json.loads(tank_profile)
        except (TypeError, ValueError):
            profile = None
        for seg in (profile or {}).get("GasProfiles") or []:
            con.execute(
                "INSERT INTO gas_segments (source_dive_id, start_s, end_s, o2_pct,"
                " he_pct, circuit, avg_depth_m, source_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    dive_id,
                    seg.get("StartTimeInSeconds"),
                    seg.get("EndTimeInSeconds"),
                    seg.get("O2Percent"),
                    seg.get("HePercent"),
                    seg.get("CircuitMode"),
                    seg.get("AverageDepthInMeters"),
                    json.dumps(seg, separators=(",", ":")),
                ),
            )

    con.executemany(
        "INSERT INTO events (source_dive_id, t_s, kind, payload_json) VALUES (?,?,?,?)",
        [
            (
                dive_id,
                e.t_s,
                "pnf_info_event",
                json.dumps(
                    {
                        "event": e.event,
                        "timestamp": e.timestamp,
                        "w1": e.w1,
                        "w2": e.w2,
                        "raw_hex": e.raw_hex,
                    },
                    separators=(",", ":"),
                ),
            )
            for e in dive.events
        ],
    )

    con.executemany(
        "INSERT INTO undecoded_payloads (source_dive_id, container, type_key, seq,"
        " payload, fields_json) VALUES (?,?,?,?,?,NULL)",
        [
            (dive_id, "pnf_record", type_key, seq, raw)
            for type_key, seq, raw in dive.unknown_records
        ],
    )

    return "ingested"
