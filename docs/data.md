---
title: Your data
description: What bottomtime stores, where it keeps it, and the columns of its exports and its SQLite tables.
---

# Your data

bottomtime keeps two things: an untouched copy of every file it reads, and a
SQLite database decoded from those copies. The copies are the record. The
database can be thrown away and rebuilt from them at any time, which is what
`bottomtime rebuild` and the app's **Rebuild from saved files** do.

## Where it lives

The app keeps everything in `~/Library/Application Support/bottomtime` on a
Mac, and in `%APPDATA%\bottomtime` on Windows:

| Path | What it is |
|---|---|
| `dives.db` | The database. |
| `archive/fit/` | Every Garmin FIT file, as Garmin sent it. |
| `archive/shearwater_db/` | Every copy of Shearwater Cloud's database that bottomtime read. |
| `archive/garmin_index/` | Garmin Connect's list of your dive activities, with their names and locations. |
| `garmin/` | Your Garmin Connect sign-in, if you signed in. |
| `settings.json` | The app's settings. |

Each archived file is named by the SHA-256 hash of its contents, so the same
file is never stored twice and a file's name proves its contents haven't
changed. The command line uses `data/dives.db` and `archive/` in the current
folder unless you pass `--db` and `--archive`.

## Exported files

**Export this dive** in the app, and `bottomtime export N`, write one CSV row
per sample per computer. The first four columns are:

| Column | Meaning |
|---|---|
| `source` | `garmin` or `shearwater`. |
| `source_dive_id` | Which log the row came from. |
| `computer` | The computer's model, such as `Perdix 2`. |
| `dive_time_s` | Seconds from the start of the dive, on the timeline every computer shares. |

The sample columns from the tables below follow. A Garmin row leaves the
Shearwater-only columns empty, and the other way round. Values are always
metric.

**Export all** writes a zip file with one such CSV per dive in a `dives`
folder, and a `dives.csv` with one row per dive: the file name, dive number,
start time in UTC, UTC offset, runtime, max depth, whether it's a pool or test
dive, the computers, the name, the mode, max GF99, GF99 at surfacing, CNS,
and minutes of deco.

## Sample columns

Every sample has `t_s`, the seconds since that computer's log started.

### Shearwater

Decoded from Shearwater's native log format by [pnf](https://github.com/snehankekre/pnf),
whose [SPEC.md](https://github.com/snehankekre/pnf/blob/main/SPEC.md) gives the
byte each value comes from. Shearwater computers record a sample every 10
seconds by default.

| Column | Meaning |
|---|---|
| `depth_m` | Depth in metres. |
| `temp_c` | Water temperature in °C. |
| `gf99` | GF99, in percent. Empty when the computer used DCIEM. |
| `ceiling_m` | The deco ceiling in metres. Empty under DCIEM. |
| `at_plus_five_min` | @+5: time to surface if you stayed five more minutes. |
| `tts_min` | Time to surface, in minutes. |
| `ndl_min` | No-decompression limit in minutes. Empty while in deco. |
| `stop_depth_m`, `stop_time_min` | The next required stop. 0 m means none. |
| `in_deco` | 1 while the computer showed a required stop. |
| `cns_pct` | CNS oxygen toxicity, in percent. |
| `avg_ppo2` | The ppO2 the computer used, in bar. |
| `sensor1_ppo2`, `sensor2_ppo2`, `sensor3_ppo2` | Each oxygen sensor's ppO2, on a CCR. |
| `sensor1_raw`, `sensor2_raw`, `sensor3_raw` | The sensors' raw readings, as the computer stored them. |
| `setpoint` | The CCR setpoint, in bar. |
| `o2_pct`, `he_pct` | The gas in use. |
| `safe_ascent_depth_m` | The safe ascent depth, under DCIEM only. |
| `battery_v`, `battery_pct` | The computer's battery voltage and percentage. |
| `tank0_psi`, `tank1_psi`, `gas_time_min`, `sac` | Air integration, when a transmitter is paired. |
| `status_flags`, `solenoid_fired_count` | The raw status byte, and the CCR oxygen solenoid count it carries. |
| `extra_json` | Anything else pnf decoded for the sample, such as extra transmitters. |

### Garmin

Read with Garmin's official FIT SDK. A Descent usually records a sample every second.

| Column | Meaning |
|---|---|
| `depth_m` | Depth in metres. |
| `temp_c` | Temperature in °C. |
| `heart_rate` | Heart rate in beats per minute. |
| `absolute_pressure` | Pressure in pascals. |
| `lat`, `lon` | Position, when Garmin puts one in a sample. Usually empty, since a watch has no GPS underwater. |
| `next_stop_depth_m`, `next_stop_time_s` | The next required stop. |
| `tts_s`, `ndl_s` | Time to surface and no-decompression limit, in seconds. |
| `cns_pct` | CNS oxygen toxicity, in percent. |
| `n2_load` | Nitrogen tissue loading, as Garmin reports it. |
| `po2` | ppO2 in bar. |
| `ascent_rate` | Ascent rate in metres per second. |
| `air_time_remaining_s`, `pressure_sac`, `volume_sac`, `rmv` | Air integration, when a transmitter is paired. |
| `extra_json` | Every other field Garmin recorded in the sample, by name or number. |

## Tables

| Table | One row per |
|---|---|
| `raw_artifacts` | Archived file: its kind, SHA-256, size, where it came from, and where it's archived. |
| `source_dives` | Log from one computer: source, serial, model, name, start time on its own clock and in UTC, runtime, max depth, mode, the decoded header verbatim, and for Shearwater the original log bytes. |
| `garmin_samples`, `shearwater_samples` | Sample, with the columns above. Never resampled. |
| `gases` | Gas a computer had set up. |
| `gas_segments` | Stretch of a dive on one gas. |
| `events` | Event a computer logged, such as a Garmin dive alarm, with its full payload. |
| `undecoded_payloads` | Record or message bottomtime doesn't understand yet, kept raw so a later version can decode it. |
| `matches` | Link between two logs of the same dive: clock offset, residual skew, cross-correlation score, and status (`auto`, `confirmed`, or `rejected`). |
| `dives` | Dive: number, start time, UTC offset, runtime, max depth, and whether it's a pool or test dive. |
| `dive_members` | Log in a dive, with `offset_s`, the seconds from the dive's start to the log's first sample. |

Two views cover common questions. `v_dive_summary` is one row per dive with
its sources, and `v_samples_unified` puts the columns both makers record
(depth, temperature, time to surface, stop depth, CNS) from every log into
one table.

`meta` holds the schema version. When a new version of bottomtime changes the
schema, it asks for a rebuild from the archive.
