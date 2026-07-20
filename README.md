# bottomtime

A lossless dive-log store for divers who log with more than one computer.

Tec divers often wear two dive computers (say, a Garmin Descent on one wrist
and a Shearwater on the other). Each records the same dive differently: the
Garmin logs at 1 Hz with heart rate, GPS, CNS and tissue loading; the
Shearwater logs its native record with GF99, deco ceiling, ppO2 sensors and
per-sample battery. No existing interchange format (UDDF, DL7, Subsurface)
preserves the union, so exporting to any of them throws data away.

bottomtime keeps everything:

- **Ingests Garmin FIT files** (official garmin-fit-sdk). Known dive channels
  become typed columns; unknown fields and Garmin's undocumented message
  types are preserved raw, per sample and per message.
- **Decodes Shearwater's Petrel Native Format** directly from a Shearwater
  Cloud database (`dive_data.db`, the "Export Database" output) with a pure
  Python decoder — including per-sample **GF99, deco ceiling, CNS and battery
  voltage** that Shearwater's own XML/CSV exports omit. Unknown record types
  are preserved raw.
- **Archives every source file verbatim**, content-addressed by SHA-256. The
  database is a decoded view; the archive is the source of truth.
- **Reconciles dual-computer dives**: interval overlap plus depth-profile
  cross-correlation links the two logs of the same physical dive (storing
  clock offset and residual skew) without ever merging or resampling the
  original series.
- **Verifies itself**: the decoder is cross-checked per-sample against
  Shearwater XML exports, against Shearwater Cloud's own computed values
  (EndGF99), and matched dives are checked for depth agreement.

Everything lands in a single SQLite file with a stable schema, ready for SQL,
pandas, or whatever you analyze with.

## Install

```sh
pip install bottomtime
```

## Usage

```sh
# create a store
bottomtime init

# ingest a directory of Garmin FIT files
# (optionally with a Garmin Connect index JSON for true UTC offsets and metadata)
bottomtime ingest garmin ~/dives/garmin-fits

# ingest a Shearwater Cloud database export
bottomtime ingest shearwater ~/dives/dive_data.db

# link dual-computer dives and build the canonical dive list
bottomtime match

# run the verification suite (XML dir optional but recommended)
bottomtime verify --xml-dir ~/dives/shearwater-xml

bottomtime status
```

All commands are idempotent: re-running an ingest skips already-stored dives,
so syncing after a dive trip only adds what's new.

## Schema in one breath

`raw_artifacts` (archived files) → `source_dives` (one row per computer log,
with the verbatim header and, for Shearwater, the raw PNF blob) →
`garmin_samples` / `shearwater_samples` (wide, per-source, never resampled) +
`gases`, `gas_segments`, `events`, `undecoded_payloads` (raw bytes of
anything not yet understood) → `matches` (dual-computer links) → `dives` +
`dive_members` (canonical dives). Views `v_dive_summary` and
`v_samples_unified` cover the common queries.

## Notes on the PNF decoder

The decoder follows libdivecomputer's `shearwater_predator_parser.c` for the
documented fields and adds empirically verified mappings for GF99 (byte 25),
deco ceiling (byte 24), battery voltage (byte 18) and @+5 TTS (bytes 26-27),
validated against Shearwater Cloud's displayed values across hundreds of
dives. Bytes without a known meaning are preserved per sample. If your dives
disagree, `bottomtime verify` will say so loudly — issue reports with a
failing blob are very welcome.

## License

MIT
