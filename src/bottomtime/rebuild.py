"""Rebuild the store from the archive.

The database is a view of the archived source files, so a new schema or a
new decoder never migrates rows in place. It ingests every archived file into
a fresh store and swaps it in only if nothing failed.
"""

from __future__ import annotations

from pathlib import Path

from .store import db as store_db


def rebuild(db_path: Path, archive_dir: Path, say=print) -> int:
    """Return the number of files that failed. The old store is replaced only
    when that is zero; otherwise the new one is left beside it."""
    from .ingest.garmin_fit import ingest_garmin_dir
    from .ingest.shearwater_db import ingest_shearwater_db
    from .match.matcher import run_match

    def oldest_first(kind: str) -> list[Path]:
        return sorted((archive_dir / kind).glob("*"), key=lambda p: p.stat().st_mtime)

    tmp = db_path.with_name(db_path.name + ".rebuild")
    tmp.unlink(missing_ok=True)
    con = store_db.connect(tmp)
    indexes = oldest_first("garmin_index")
    stats = ingest_garmin_dir(
        con, archive_dir, archive_dir / "fit", indexes[-1] if indexes else None
    )
    say(f"garmin: {stats}")
    failed = stats["failed"]
    # Each Cloud export holds every dive so far, so older exports go first and
    # later ones only add the dives that are new.
    for path in oldest_first("shearwater_db"):
        stats = ingest_shearwater_db(con, archive_dir, path)
        say(f"shearwater {path.name[:12]}: {stats}")
        failed += stats["failed"]
    say(f"match: {run_match(con, verbose=False)}")
    con.close()
    if failed:
        say(f"{failed} file(s) failed; {db_path} is unchanged, the new store is {tmp}")
        return failed
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    tmp.replace(db_path)
    say(f"rebuilt {db_path}")
    return 0
