"""Content-addressed archive of verbatim raw source files.

Every ingested file is copied (if absent) to <archive_dir>/<kind>/<sha256><ext>
and registered in raw_artifacts. The archive, not the database, is the
byte-level source of truth; re-running is a no-op.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from ..store.db import utcnow


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_file(
    con: sqlite3.Connection, archive_dir: Path, kind: str, path: Path
) -> tuple[int, str]:
    """Archive path under kind; return (artifact_id, sha256)."""
    digest = sha256_file(path)
    row = con.execute(
        "SELECT id FROM raw_artifacts WHERE sha256=?", (digest,)
    ).fetchone()
    if row:
        return row[0], digest

    dest_dir = archive_dir / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (digest + path.suffix.lower())
    if not dest.exists():
        shutil.copy2(path, dest)

    cur = con.execute(
        "INSERT INTO raw_artifacts (kind, sha256, size_bytes, original_path, archive_path, imported_at)"
        " VALUES (?,?,?,?,?,?)",
        (kind, digest, path.stat().st_size, str(path), str(dest), utcnow()),
    )
    return cur.lastrowid, digest
