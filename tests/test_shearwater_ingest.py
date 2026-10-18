"""Shearwater ingest against a minimal Shearwater Cloud database."""

import gzip
import shutil
import sqlite3
import struct

from bottomtime.ingest.shearwater_db import ingest_shearwater_db
from bottomtime.store import db as store_db


def _blob(dive_number=13, start=1758974143, depths_dm=(30, 300, 30)):
    records = []
    o0 = bytearray(32)
    o0[0] = 0x10
    struct.pack_into(">H", o0, 2, dive_number)
    o0[4], o0[5], o0[20] = 40, 80, 21
    struct.pack_into(">I", o0, 12, start)
    records.append(o0)
    for t in range(1, 5):
        rec = bytearray(32)
        rec[0] = 0x10 + t
        records.append(rec)
    records[4][16] = 13  # log version
    for depth in depths_dm:
        s = bytearray(32)
        s[0] = 0x01
        struct.pack_into(">H", s, 1, depth)
        s[8], s[12], s[24], s[25] = 21, 0x10, 0xFF, 0xFF
        struct.pack_into(">H", s, 20, 0xFFFF)
        struct.pack_into(">H", s, 28, 0xFFFF)
        struct.pack_into(">H", s, 30, 0xFFFF)
        records.append(s)
    for t in range(5):
        rec = bytearray(32)
        rec[0] = 0x20 + t
        records.append(rec)
    final = bytearray(32)
    final[0], final[1], final[13] = 0xFF, 0xFD, 11
    struct.pack_into(">I", final, 2, 0xA823C228)
    records.append(final)
    raw = b"".join(bytes(r) for r in records)
    return struct.pack("<I", len(raw)) + gzip.compress(raw)


def _cloud_db(path, blob):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE log_data (log_id TEXT, file_name TEXT, format TEXT, format_version INTEGER,"
        " calculated_values_from_samples TEXT, data_bytes_1 BLOB, data_bytes_2 TEXT, data_bytes_3 TEXT)"
    )
    con.execute("CREATE TABLE dive_details (DiveId TEXT)")
    con.execute(
        "INSERT INTO log_data VALUES ('1', 'Perdix 2#13', 'sw-pnf', 14, NULL, ?, NULL, NULL)", (blob,)
    )
    con.commit()
    con.close()


def test_identity_and_start_come_from_the_blob(tmp_path):
    _cloud_db(tmp_path / "dive_data.db", _blob())
    con = store_db.connect(tmp_path / "store.db")
    assert ingest_shearwater_db(con, tmp_path / "archive", tmp_path / "dive_data.db")["ingested"] == 1
    row = con.execute("SELECT source_key, device, model, start_time_local FROM source_dives").fetchone()
    assert tuple(row) == ("A823C228#13", "shearwater:A823C228", "Perdix 2", "2025-09-27 11:55:43")


def test_reading_the_same_database_again_skips_it(tmp_path):
    # The same Cloud data under a new path, as each app read makes a new snapshot.
    _cloud_db(tmp_path / "first.db", _blob())
    shutil.copy(tmp_path / "first.db", tmp_path / "second.db")
    con = store_db.connect(tmp_path / "store.db")
    ingest_shearwater_db(con, tmp_path / "archive", tmp_path / "first.db")
    stats = ingest_shearwater_db(con, tmp_path / "archive", tmp_path / "second.db")
    assert stats == {"ingested": 0, "skipped": 1, "failed": 0}


def test_a_recompressed_blob_is_the_same_dive(tmp_path):
    # Cloud re-gzips dives between exports; the gzip header changes, the records don't.
    blob = _blob()
    regzipped = blob[:4] + gzip.compress(gzip.decompress(blob[4:]), mtime=1)
    assert regzipped != blob
    _cloud_db(tmp_path / "july.db", blob)
    _cloud_db(tmp_path / "august.db", regzipped)
    con = store_db.connect(tmp_path / "store.db")
    ingest_shearwater_db(con, tmp_path / "archive", tmp_path / "july.db")
    assert ingest_shearwater_db(con, tmp_path / "archive", tmp_path / "august.db")["skipped"] == 1
