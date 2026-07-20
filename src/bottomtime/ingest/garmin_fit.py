"""Ingest Garmin Descent FIT activity files.

Decodes with the official garmin-fit-sdk. Known dive channels land in typed
columns of garmin_samples; every other field (named or unknown-numeric) is
preserved per sample in extra_json. Whole message types the FIT profile does
not know (233, 325, ...) are preserved per occurrence in undecoded_payloads.

An optional Garmin Connect index JSON (a list of activity dicts with
activityId, startTimeGMT, startTimeLocal, ...) supplies true UTC offsets and
Connect metadata; without it, times fall back to FIT timestamps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garmin_fit_sdk import Decoder, Stream

from ..store.db import source_dive_exists, utcnow
from .archive import archive_file

DECODER_VERSION = 1

FIT_EPOCH_OFFSET = 631065600  # FIT timestamps count from 1989-12-31 00:00 UTC
SEMICIRCLE = 180.0 / 2**31

# record message field -> garmin_samples column
RECORD_COLUMNS = {
    "depth": "depth_m",
    "temperature": "temp_c",
    "heart_rate": "heart_rate",
    "absolute_pressure": "absolute_pressure",
    "next_stop_depth": "next_stop_depth_m",
    "next_stop_time": "next_stop_time_s",
    "time_to_surface": "tts_s",
    "ndl_time": "ndl_s",
    "cns_load": "cns_pct",
    "n2_load": "n2_load",
    "po2": "po2",
    "ascent_rate": "ascent_rate",
    "air_time_remaining": "air_time_remaining_s",
    "pressure_sac": "pressure_sac",
    "volume_sac": "volume_sac",
    "rmv": "rmv",
}

# message types routed to dedicated tables rather than header_json
NON_HEADER_KEYS = {"record_mesgs", "event_mesgs", "dive_alarm_mesgs", "dive_gas_mesgs"}


def _jsonable(value):
    if isinstance(value, (bytes, bytearray)):
        return {"__hex__": bytes(value).hex()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def load_index(index_path: Path | None) -> dict[str, dict]:
    if not index_path or not index_path.exists():
        return {}
    entries = json.loads(index_path.read_text())
    return {str(e["activityId"]): e for e in entries}


def _parse_connect_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def ingest_fit_file(
    con: sqlite3.Connection,
    archive_dir: Path,
    path: Path,
    index: dict[str, dict],
) -> str:
    """Ingest one FIT file. Returns 'ingested' | 'skipped'."""
    artifact_id, digest = archive_file(con, archive_dir, "fit", path)
    if source_dive_exists(con, "garmin", digest):
        return "skipped"

    stream = Stream.from_file(str(path))
    decoder = Decoder(stream)
    messages, errors = decoder.read(convert_datetimes_to_dates=False)
    if errors:
        raise ValueError(f"FIT decode errors in {path.name}: {errors}")

    records = messages.get("record_mesgs", [])
    # A file can carry summaries but no sample stream (rare device hiccup);
    # keep it as a header-only source dive rather than losing the dive.
    if records:
        t0 = records[0]["timestamp"]
    else:
        session = (messages.get("session_mesgs") or [{}])[0]
        file_id_msg = (messages.get("file_id_mesgs") or [{}])[0]
        t0 = session.get("start_time") or file_id_msg.get("time_created")
        if t0 is None:
            raise ValueError(f"no records and no start time in {path.name}")

    file_id = (messages.get("file_id_mesgs") or [{}])[0]
    activity_id = str(file_id.get("serial_number", ""))
    # Garmin Connect's activityId is not in the file; recover it from the
    # index by matching start time, else from a '<...>_<id>.fit' filename.
    entry = None
    stem_id = path.stem.rsplit("_", 1)[-1]
    if stem_id.isdigit() and stem_id in index:
        entry = index[stem_id]
        activity_id = stem_id
    else:
        start_utc_probe = datetime.fromtimestamp(
            t0 + FIT_EPOCH_OFFSET, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        for aid, e in index.items():
            if e.get("startTimeGMT") == start_utc_probe:
                entry, activity_id = e, aid
                break

    start_dt_utc = datetime.fromtimestamp(t0 + FIT_EPOCH_OFFSET, tz=timezone.utc)
    start_utc = start_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
    start_local, utc_offset_min = start_utc, None
    if entry:
        gmt = _parse_connect_time(entry["startTimeGMT"])
        local = _parse_connect_time(entry["startTimeLocal"])
        utc_offset_min = int((local - gmt).total_seconds() // 60)
        start_local = entry["startTimeLocal"]
    else:
        # activity message carries the device's local timestamp
        act = (messages.get("activity_mesgs") or [{}])[0]
        if "local_timestamp" in act and "timestamp" in act:
            utc_offset_min = int((act["local_timestamp"] - act["timestamp"]) // 60)
            start_local = (
                start_dt_utc + timedelta(minutes=utc_offset_min)
            ).strftime("%Y-%m-%d %H:%M:%S")

    header = {"connect_index": entry}
    for key, msgs in messages.items():
        if isinstance(key, str) and key.endswith("_mesgs") and key not in NON_HEADER_KEYS:
            header[key] = _jsonable(msgs)

    summaries = messages.get("dive_summary_mesgs") or []
    session = (messages.get("session_mesgs") or [{}])[0]
    duration = entry.get("duration") if entry else session.get("total_elapsed_time")
    max_depth = max((r.get("depth") or 0 for r in records), default=None)
    if max_depth is None and session.get("max_depth") is not None:
        max_depth = session["max_depth"]
    avg_depth = next(
        (s.get("avg_depth") for s in summaries if s.get("avg_depth") is not None), None
    )
    mode = entry.get("activityType", {}).get("typeKey") if entry else None

    intervals = [
        records[i + 1]["timestamp"] - records[i]["timestamp"]
        for i in range(min(len(records) - 1, 50))
    ]
    interval_ms = int(sorted(intervals)[len(intervals) // 2] * 1000) if intervals else None

    cur = con.execute(
        "INSERT INTO source_dives (source, artifact_id, source_key, content_hash,"
        " decoder_version, start_time_utc, start_time_local, utc_offset_min,"
        " duration_s, max_depth_m, avg_depth_m, sample_interval_ms, mode, header_json)"
        " VALUES ('garmin',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            artifact_id,
            activity_id,
            digest,
            DECODER_VERSION,
            start_utc,
            start_local,
            utc_offset_min,
            duration,
            max_depth,
            avg_depth,
            interval_ms,
            mode,
            json.dumps(_jsonable(header), separators=(",", ":")),
        ),
    )
    dive_id = cur.lastrowid

    sample_rows = []
    for rec in records:
        row = {col: None for col in RECORD_COLUMNS.values()}
        extra = {}
        lat = lon = None
        for k, v in rec.items():
            if k == "timestamp":
                continue
            elif k == "position_lat":
                lat = v * SEMICIRCLE
            elif k == "position_long":
                lon = v * SEMICIRCLE
            elif isinstance(k, str) and k in RECORD_COLUMNS:
                row[RECORD_COLUMNS[k]] = v
            else:
                extra[str(k)] = _jsonable(v)
        sample_rows.append(
            (
                dive_id,
                float(rec["timestamp"] - t0),
                row["depth_m"], row["temp_c"], row["heart_rate"],
                row["absolute_pressure"], lat, lon,
                row["next_stop_depth_m"], row["next_stop_time_s"], row["tts_s"],
                row["ndl_s"], row["cns_pct"], row["n2_load"], row["po2"],
                row["ascent_rate"], row["air_time_remaining_s"],
                row["pressure_sac"], row["volume_sac"], row["rmv"],
                json.dumps(extra, separators=(",", ":")) if extra else None,
            )
        )
    con.executemany(
        "INSERT INTO garmin_samples (source_dive_id, t_s, depth_m, temp_c, heart_rate,"
        " absolute_pressure, lat, lon, next_stop_depth_m, next_stop_time_s, tts_s,"
        " ndl_s, cns_pct, n2_load, po2, ascent_rate, air_time_remaining_s,"
        " pressure_sac, volume_sac, rmv, extra_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        sample_rows,
    )

    for gi, gas in enumerate(messages.get("dive_gas_mesgs") or []):
        mode_str = str(gas.get("mode", ""))
        circuit = {"open_circuit": "oc", "closed_circuit": "cc"}.get(mode_str)
        con.execute(
            "INSERT INTO gases (source_dive_id, gas_index, o2_pct, he_pct, circuit,"
            " enabled, used, source_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                dive_id,
                gas.get("message_index", gi),
                gas.get("oxygen_content"),
                gas.get("helium_content"),
                circuit,
                1 if gas.get("status") == "enabled" else 0,
                None,
                json.dumps(_jsonable(gas), separators=(",", ":")),
            ),
        )

    event_rows = []
    for kind, key in (("fit_event", "event_mesgs"), ("fit_dive_alarm", "dive_alarm_mesgs")):
        for msg in messages.get(key) or []:
            ts = msg.get("timestamp")
            event_rows.append(
                (
                    dive_id,
                    float(ts - t0) if isinstance(ts, (int, float)) else None,
                    kind,
                    json.dumps(_jsonable(msg), separators=(",", ":")),
                )
            )
    con.executemany(
        "INSERT INTO events (source_dive_id, t_s, kind, payload_json) VALUES (?,?,?,?)",
        event_rows,
    )

    undecoded = []
    for key, msgs in messages.items():
        if isinstance(key, str) and key.endswith("_mesgs"):
            continue
        for seq, msg in enumerate(msgs):
            undecoded.append(
                (
                    dive_id,
                    "fit_mesg",
                    str(key),
                    seq,
                    None,
                    json.dumps(_jsonable(msg), separators=(",", ":")),
                )
            )
    con.executemany(
        "INSERT INTO undecoded_payloads (source_dive_id, container, type_key, seq,"
        " payload, fields_json) VALUES (?,?,?,?,?,?)",
        undecoded,
    )

    return "ingested"


def ingest_garmin_dir(
    con: sqlite3.Connection,
    archive_dir: Path,
    fit_dir: Path,
    index_name: str = "dives_index.json",
) -> dict:
    index_path = fit_dir / index_name
    index = load_index(index_path)
    if index_path.exists():
        archive_file(con, archive_dir, "garmin_index", index_path)

    stats = {"ingested": 0, "skipped": 0, "failed": 0}
    for path in sorted(fit_dir.glob("*.fit")):
        try:
            with con:
                result = ingest_fit_file(con, archive_dir, path, index)
            stats[result] += 1
        except Exception as e:
            stats["failed"] += 1
            print(f"FAILED {path.name}: {type(e).__name__}: {e}")
    return stats
