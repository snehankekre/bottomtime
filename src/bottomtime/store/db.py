"""Connection and small helpers for the dive store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schema import DDL, SCHEMA_VERSION


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def source_dive_exists(con: sqlite3.Connection, source: str, content_hash: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM source_dives WHERE source=? AND content_hash=?",
        (source, content_hash),
    ).fetchone()
    return row is not None
