"""bottomtime command line interface.

    bottomtime init
    bottomtime ingest garmin <fit-dir>
    bottomtime ingest shearwater <dive_data.db>
    bottomtime match
    bottomtime verify [--xml-dir DIR]
    bottomtime status

All commands are idempotent: re-running an ingest skips anything already
stored (content-hash keyed), match recomputes only 'auto' links.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .store import db as store_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bottomtime")
    parser.add_argument("--db", default="data/dives.db", help="path to the dive store")
    parser.add_argument(
        "--archive", default="archive", help="directory for verbatim raw source copies"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty dive store")

    p_ingest = sub.add_parser("ingest", help="ingest a data source")
    ingest_sub = p_ingest.add_subparsers(dest="source", required=True)
    p_garmin = ingest_sub.add_parser("garmin", help="a directory of FIT files")
    p_garmin.add_argument("fit_dir", type=Path)
    p_garmin.add_argument(
        "--index",
        default="dives_index.json",
        help="Garmin Connect index JSON filename inside fit-dir",
    )
    p_sw = ingest_sub.add_parser("shearwater", help="a Shearwater Cloud dive_data.db")
    p_sw.add_argument("db_path", type=Path)

    sub.add_parser("match", help="match dual-computer dives, build canonical dives")

    p_verify = sub.add_parser("verify", help="run the verification suite")
    p_verify.add_argument("--xml-dir", type=Path, default=None,
                          help="directory of Shearwater XML exports to cross-check")

    sub.add_parser("status", help="summarize the store")

    args = parser.parse_args(argv)
    con = store_db.connect(args.db)
    archive_dir = Path(args.archive)

    if args.command == "init":
        print(f"initialized {args.db}")
        return 0

    if args.command == "ingest" and args.source == "garmin":
        from .ingest.garmin_fit import ingest_garmin_dir

        stats = ingest_garmin_dir(con, archive_dir, args.fit_dir, args.index)
        print(f"garmin: {stats}")
        return 1 if stats["failed"] else 0

    if args.command == "ingest" and args.source == "shearwater":
        from .ingest.shearwater_db import ingest_shearwater_db

        stats = ingest_shearwater_db(con, archive_dir, args.db_path)
        print(f"shearwater: {stats}")
        return 1 if stats["failed"] else 0

    if args.command == "match":
        from .match.matcher import run_match

        stats = run_match(con)
        print(f"match: {stats}")
        return 0

    if args.command == "verify":
        from .verify.crosscheck import run_verify

        return run_verify(con, args.xml_dir)

    if args.command == "status":
        for line in con.execute(
            "SELECT source || ' source dives: ' || count(*) FROM source_dives GROUP BY source"
        ):
            print(line[0])
        for line in con.execute(
            "SELECT 'canonical dives: ' || count(*) || ' (' ||"
            " sum(CASE WHEN is_test THEN 1 ELSE 0 END) || ' test)' FROM dives"
        ):
            print(line[0])
        for line in con.execute(
            "SELECT 'matched pairs: ' || count(*) FROM matches WHERE status IN ('auto','confirmed')"
        ):
            print(line[0])
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
