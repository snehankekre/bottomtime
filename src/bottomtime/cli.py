"""bottomtime command line interface.

    bottomtime init
    bottomtime ingest garmin <fit-dir>
    bottomtime ingest shearwater <dive_data.db>
    bottomtime match
    bottomtime verify [--xml-dir DIR]
    bottomtime status
    bottomtime list [--all]
    bottomtime show <dive-number>
    bottomtime export <dive-number> [--format csv|json] [-o FILE]
    bottomtime plot <dive-number> [-o FILE]

All commands are idempotent: re-running an ingest skips anything already
stored (content-hash keyed), match recomputes only 'auto' links.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .store import db as store_db


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_local(start_utc, utc_offset_min, fmt="%Y-%m-%d %H:%M") -> str:
    if start_utc is None:
        return "?"
    dt = datetime.strptime(start_utc, "%Y-%m-%d %H:%M:%S")
    if utc_offset_min is not None:
        dt += timedelta(minutes=utc_offset_min)
    return dt.strftime(fmt)


def _fmt_offset(utc_offset_min) -> str:
    if utc_offset_min is None:
        return "UTC?"
    sign = "+" if utc_offset_min >= 0 else "-"
    h, m = divmod(abs(utc_offset_min), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def _iter_source_entries(dive: dict):
    for source, value in dive["sources"].items():
        for entry in value if isinstance(value, list) else [value]:
            yield source, entry


def _computer_label(info: dict) -> str:
    parts = [info.get("model") or "unknown"]
    if info.get("firmware") is not None:
        parts.append(str(info["firmware"]))
    return " ".join(parts)


def _cmd_list(con, include_tests: bool) -> int:
    from .api import computer_info

    where = "" if include_tests else "WHERE is_test = 0"
    dives = con.execute(
        f"SELECT * FROM v_dive_summary {where} ORDER BY start_time_utc"
    ).fetchall()
    members = {
        r["id"]: r
        for r in con.execute(
            "SELECT id, source, mode, header_json FROM source_dives"
        )
    }
    rows = []
    for d in dives:
        computers, mode = [], None
        for sid in (int(i) for i in d["source_dive_ids"].split(",")):
            sd = members[sid]
            info = computer_info(sd["source"], json.loads(sd["header_json"]))
            computers.append(info.get("model") or sd["source"])
            if sd["source"] == "shearwater" or mode is None:
                mode = sd["mode"] or mode
        depth = f"{d['max_depth_m']:.1f}" if d["max_depth_m"] is not None else "?"
        rows.append(
            (
                "T" if d["is_test"] else str(d["dive_number"]),
                _fmt_local(d["start_time_utc"], d["utc_offset_min"]),
                _fmt_duration(d["duration_s"]),
                depth,
                mode or "?",
                " + ".join(sorted(set(computers))),
            )
        )
    if not rows:
        print("no dives (run ingest + match first)")
        return 0
    header = ("#", "start (local)", "duration", "max m", "mode", "computers")
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    fmt = "  ".join(
        f"{{:{'>' if i in (0, 2, 3) else '<'}{widths[i]}}}" for i in range(len(header))
    )
    print(fmt.format(*header))
    for r in rows:
        print(fmt.format(*r))
    return 0


def _cmd_show(db_path: str, dive_number: int) -> int:
    from .api import load_dive

    dive = load_dive(db_path, dive_number)
    depth = f"{dive['max_depth_m']:.1f} m" if dive["max_depth_m"] is not None else "?"
    print(
        f"dive {dive['dive_number']}  "
        f"{_fmt_local(dive['start_time_utc'], dive['utc_offset_min'])} "
        f"({_fmt_offset(dive['utc_offset_min'])})  "
        f"duration {_fmt_duration(dive['duration_s'])}  max depth {depth}"
    )
    for source, e in _iter_source_entries(dive):
        info = e["computer"]
        serial = f"  serial {info['serial']}" if info.get("serial") else ""
        print(f"\n  [{source}] {_computer_label(info)}{serial}"
              f"  (source dive {e['source_dive_id']}, key {e['source_key']})")
        interval = (
            f"{e['sample_interval_ms'] / 1000:g} s"
            if e["sample_interval_ms"] is not None
            else "?"
        )
        n = len(e["samples"].get("t_s", []))
        e_depth = f"{e['max_depth_m']:.1f} m" if e["max_depth_m"] is not None else "?"
        print(
            f"    start {e['start_time_local']} local  "
            f"duration {_fmt_duration(e['duration_s'])}  max {e_depth}  "
            f"mode {e['mode'] or '?'}  {n} samples @ {interval}"
        )
        channels = sorted(
            c
            for c, vals in e["samples"].items()
            if c not in ("t_s", "extra_json") and any(v is not None for v in vals)
        )
        if channels:
            print(f"    channels: {', '.join(channels)}")
        gases = [
            f"{(g['o2_pct'] or 0):g}/{(g['he_pct'] or 0):g}"
            + (f" {g['circuit']}" if g["circuit"] else "")
            for g in e["gases"]
            if g["o2_pct"] or g["he_pct"]
        ]
        if gases:
            print(f"    gases: {', '.join(gases)}")
        if e["events"]:
            print(f"    events: {len(e['events'])}")
    for m in dive["matches"]:
        skew = f"{m['residual_skew_s']:g} s" if m["residual_skew_s"] is not None else "?"
        score = f"{m['xcorr_score']:.3f}" if m["xcorr_score"] is not None else "?"
        delta = (
            f"{m['max_depth_delta_m']:.2f} m"
            if m["max_depth_delta_m"] is not None
            else "?"
        )
        print(
            f"\n  match garmin#{m['garmin_source_dive_id']} <-> "
            f"shearwater#{m['shearwater_source_dive_id']}: "
            f"clock offset {m['clock_offset_s']:+d} s, xcorr {score}, "
            f"residual skew {skew}, depth delta {delta} ({m['status']})"
        )
    return 0


def _cmd_export(db_path: str, dive_number: int, fmt: str, out: Path | None) -> int:
    from .api import load_dive

    dive = load_dive(db_path, dive_number)
    if fmt == "json":
        text = json.dumps(dive, separators=(",", ":"), default=str)
        if out:
            out.write_text(text)
            print(f"wrote {out}")
        else:
            print(text)
        return 0

    import csv

    columns: list[str] = []
    for _source, e in _iter_source_entries(dive):
        for c in e["samples"]:
            if c not in columns:
                columns.append(c)
    fh = out.open("w", newline="") if out else sys.stdout
    try:
        writer = csv.writer(fh)
        writer.writerow(["source", "source_dive_id"] + columns)
        for source, e in _iter_source_entries(dive):
            samples = e["samples"]
            n = len(samples.get("t_s", []))
            for i in range(n):
                writer.writerow(
                    [source, e["source_dive_id"]]
                    + [
                        samples[c][i] if c in samples else None
                        for c in columns
                    ]
                )
    finally:
        if out:
            fh.close()
            print(f"wrote {out}")
    return 0


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

    p_list = sub.add_parser("list", help="list canonical dives")
    p_list.add_argument("--all", action="store_true", help="include test dives")

    p_show = sub.add_parser("show", help="show one dive in detail")
    p_show.add_argument("dive_number", type=int)

    p_export = sub.add_parser("export", help="export a dive's sample series")
    p_export.add_argument("dive_number", type=int)
    p_export.add_argument("--format", choices=("csv", "json"), default="csv")
    p_export.add_argument("-o", "--out", type=Path, default=None)

    p_plot = sub.add_parser("plot", help="plot a dive (requires matplotlib)")
    p_plot.add_argument("dive_number", type=int)
    p_plot.add_argument("-o", "--out", type=Path, default=None,
                        help="write an image instead of opening a window")

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

    if args.command == "list":
        return _cmd_list(con, args.all)

    if args.command in ("show", "export", "plot"):
        try:
            if args.command == "show":
                return _cmd_show(args.db, args.dive_number)
            if args.command == "export":
                return _cmd_export(args.db, args.dive_number, args.format, args.out)
            from .plot import plot_dive

            plot_dive(args.db, args.dive_number, args.out)
            return 0
        except KeyError as e:
            print(e.args[0] if e.args else e, file=sys.stderr)
            return 1

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
