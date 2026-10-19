---
title: Command line and Python
description: Install bottomtime from PyPI, build a dive store from the command line, and read it from Python or SQL.
---

# Command line and Python

The app and the command line use the same package and the same store
format. The command line works anywhere Python 3.10 or newer runs, including
Linux and Windows.

## Install

```sh
pip install bottomtime
```

Two extras add optional parts:

```sh
pip install 'bottomtime[plot]'   # matplotlib, for `bottomtime plot`
pip install 'bottomtime[app]'    # pywebview and garminconnect, for `bottomtime app`
```

## Building a store

```sh
# create an empty store
bottomtime init

# a directory of Garmin FIT files. A Garmin Connect index JSON inside it
# (dives_index.json, or pass --index NAME) adds activity ids and names.
bottomtime ingest garmin ~/dives/garmin-fits

# a Shearwater Cloud database: Shearwater Cloud's File > Export > Export
# Database, or the live dive_data.db the desktop app keeps
bottomtime ingest shearwater ~/dives/dive_data.db

# link the logs that recorded the same dive, and number the dives
bottomtime match

# check the decoder and the matches (the XML directory is optional)
bottomtime verify --xml-dir ~/dives/shearwater-xml

bottomtime status
```

Two options go before the command, as in `bottomtime --db PATH list`.
`--db` is the store (default `data/dives.db`). `--archive` is the folder
where bottomtime keeps its copy of every file it reads (default `archive`).

Every ingest is safe to repeat. bottomtime recognises files and dives it
already has by their content, so ingesting a folder again after a dive trip
only adds what's new. `match` recomputes its automatic links each time and
leaves links you confirmed or rejected alone.

## After upgrading

```sh
bottomtime rebuild
```

`rebuild` builds a fresh store from every file in the archive and replaces the
old store only if nothing failed. bottomtime refuses to open a store built by
an older version, and says to run `rebuild`. The app does this by itself.

## Looking at your dives

```sh
bottomtime list                 # every dive (--all includes pool and test dives)
bottomtime show 291             # one dive: computers, channels, gases, match quality
bottomtime export 291 -o d.csv  # every sample from every computer (or --format json)
bottomtime plot 291 -o d.png    # both profiles on one timeline, with GF99 and ppO2
```

`export` writes one row per sample per computer. The `dive_time_s` column puts
every computer on the dive's shared timeline, using the offsets the matcher
stored, so nothing is resampled. [Your data](data.md) lists every column.

## The app from source

```sh
bottomtime app              # the desktop window
bottomtime app --browser    # the same app in a browser tab
bottomtime app --data-dir ~/some/folder
```

The app keeps its own store in `~/Library/Application Support/bottomtime` on
a Mac and in `%APPDATA%\bottomtime` on Windows, separate from the
`data/dives.db` the command line uses by default.
`packaging/build_mac_app.sh` in the repository builds the Mac app, and the
`windows app` workflow in `.github/workflows` builds the Windows one.

## Python

```python
import bottomtime
import pandas as pd

for d in bottomtime.list_dives("data/dives.db"):
    print(d["dive_number"], d["start_time_utc"], d["max_depth_m"], d["sources"])

dive = bottomtime.load_dive("data/dives.db", 291)
sw = pd.DataFrame(dive["sources"]["shearwater"][0]["samples"])
sw.plot(x="t_s", y=["depth_m", "ceiling_m"])
```

`list_dives(db, include_tests=False)` returns one dict per dive with the
columns of the `dives` table and a comma-separated `sources`.

`load_dive(db, dive_number)` returns everything about one dive. Pass
`dive_id=` instead to load a pool or test dive, which has no number. The
result has:

| Key | What it holds |
|---|---|
| `dive_number`, `start_time_utc`, `utc_offset_min`, `duration_s`, `max_depth_m`, `is_test` | The dive itself. |
| `sources` | A dict from source (`"garmin"`, `"shearwater"`) to a list of that source's logs. It's a list because one Garmin activity can span several Shearwater logs, such as a pool session. |
| `matches` | The links between the logs: clock offset, residual skew, cross-correlation score, and status. |

Each log in `sources` has:

| Key | What it holds |
|---|---|
| `samples` | Column-oriented samples: a dict from column name to a list, ready for `pd.DataFrame`. |
| `offset_s` | Seconds from the dive's start to this log's `t_s = 0`. Add it to `t_s` to put logs on one timeline. |
| `computer` | Model, firmware, and serial number. |
| `gases`, `events` | The gases the computer had and the events it logged. |
| `header` | The decoded header, verbatim. |
| `start_time_utc`, `start_time_local`, `duration_s`, `max_depth_m`, `sample_interval_ms`, `mode`, `device`, `name` | The log's own numbers. |

`compare_logs(load_dive(db, n))` returns what the dive's computers said side
by side: each log's water density, gradient factors, max depth, minutes of
required stops, CNS at the end, and runtime, and for each log after the first,
how much deeper it read than the first (`pct` and `m`, medians below 5 m) and
how much of that the water settings predict (`settings_pct`). It returns None
for a dive with one log.

## SQL

The store is one SQLite file, so any SQLite tool reads it:

```sh
sqlite3 data/dives.db "SELECT dive_number, max_depth_m FROM v_dive_summary
                       WHERE is_test = 0 ORDER BY max_depth_m DESC LIMIT 10"
datasette data/dives.db   # a web UI and JSON API, if you have datasette
```

[Your data](data.md) describes every table.
