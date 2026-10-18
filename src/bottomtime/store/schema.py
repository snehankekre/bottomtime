"""SQLite schema for the canonical dive store.

Design principles:
- Raw source files are archived verbatim (content-addressed) and referenced
  from raw_artifacts; the database holds decoded views of them.
- One row per source log in source_dives; per-source sample tables are wide
  (each source has a fixed channel vocabulary) and never resampled.
- Anything not understood is preserved in undecoded_payloads.
- The database is disposable. When the schema or a decoder changes,
  `bottomtime rebuild` re-ingests everything from the archive.
- matches links two source dives, from different devices, that recorded the
  same physical dive; dives is the canonical entity built from matches +
  singletons.
- Adding a computer type means a samples table, an ingest module, and an
  entry in SOURCES.
"""

SCHEMA_VERSION = 5

# Every source kind: its samples table, and what its start time means. 'utc'
# logs carry true UTC (Garmin). 'local' logs carry a wall clock with no zone
# (Shearwater), so their UTC comes from a match or from the diver's usual
# offset.
SOURCES = {
    "garmin": {"samples": "garmin_samples", "clock": "utc"},
    "shearwater": {"samples": "shearwater_samples", "clock": "local"},
}

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_artifacts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'fit' | 'shearwater_db' | 'xml' | 'garmin_index'
  sha256 TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL,
  original_path TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_dives (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'garmin' | 'shearwater'
  artifact_id INTEGER NOT NULL REFERENCES raw_artifacts(id),
  source_key TEXT NOT NULL,        -- garmin activityId | shearwater '<serial>#<diveNumber>'
  device TEXT NOT NULL,            -- '<source>:<serial>', one per physical computer
  model TEXT,                      -- e.g. 'Perdix 2', 'Descent Mk3'
  name TEXT,                       -- the diver's or Garmin's name for it, if not generic
  content_hash TEXT NOT NULL,
  decoder_version TEXT NOT NULL,   -- package that decoded it, e.g. 'pnf 0.2.0' 
  start_time_utc TEXT,             -- 'local' clocks: NULL until a match gives the offset
  start_time_local TEXT NOT NULL,
  utc_offset_min INTEGER,
  duration_s REAL,
  max_depth_m REAL,
  avg_depth_m REAL,
  sample_interval_ms INTEGER,
  mode TEXT,
  header_json TEXT NOT NULL,       -- verbatim per-source header/metadata
  raw_blob BLOB,                   -- shearwater PNF gzip blob verbatim; NULL for garmin
  UNIQUE (source, content_hash),
  UNIQUE (source, source_key)
);

CREATE TABLE IF NOT EXISTS garmin_samples (
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  t_s REAL NOT NULL,
  depth_m REAL,
  temp_c REAL,
  heart_rate INTEGER,
  absolute_pressure REAL,          -- Pa
  lat REAL,
  lon REAL,
  next_stop_depth_m REAL,
  next_stop_time_s REAL,
  tts_s REAL,
  ndl_s REAL,
  cns_pct REAL,
  n2_load REAL,
  po2 REAL,
  ascent_rate REAL,                -- m/s
  air_time_remaining_s REAL,
  pressure_sac REAL,
  volume_sac REAL,
  rmv REAL,
  extra_json TEXT,
  PRIMARY KEY (source_dive_id, t_s)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS shearwater_samples (
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  t_s REAL NOT NULL,
  depth_m REAL,
  temp_c REAL,
  stop_depth_m REAL,               -- first/deepest stop depth; 0 = no stop
  ndl_min REAL,                    -- NULL while in deco
  stop_time_min REAL,              -- NULL when there is no stop
  tts_min REAL,
  at_plus_five_min INTEGER,        -- TTS if the diver stayed 5 more minutes
  in_deco INTEGER,
  avg_ppo2 REAL,
  o2_pct REAL,
  he_pct REAL,
  setpoint REAL,
  cns_pct REAL,
  gf99 REAL,                       -- NULL under DCIEM
  ceiling_m REAL,                  -- NULL under DCIEM
  safe_ascent_depth_m REAL,        -- DCIEM only (bytes 24-25)
  battery_pct INTEGER,             -- battery percent remaining (byte 11)
  sac REAL,                        -- surface air consumption (bytes 30-31)
  sensor1_raw INTEGER,
  sensor2_raw INTEGER,
  sensor3_raw INTEGER,
  sensor1_ppo2 REAL,
  sensor2_ppo2 REAL,
  sensor3_ppo2 REAL,
  battery_v REAL,
  status_flags INTEGER,             -- raw sample byte 12 (packed mode/status bits)
  solenoid_fired_count INTEGER,     -- CCR O2 solenoid firings, byte 12 bits 6-7
  tank0_psi REAL,
  tank1_psi REAL,
  gas_time_min REAL,
  extra_json TEXT,                 -- pnf Sample.extra: extra AI tanks, transmitter battery
  PRIMARY KEY (source_dive_id, t_s)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS gases (
  id INTEGER PRIMARY KEY,
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  gas_index INTEGER,
  o2_pct REAL,
  he_pct REAL,
  circuit TEXT,                    -- 'oc' | 'cc' (diluent) | NULL
  enabled INTEGER,
  source_json TEXT
);

CREATE TABLE IF NOT EXISTS gas_segments (
  id INTEGER PRIMARY KEY,
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  start_s REAL,
  end_s REAL,
  o2_pct REAL,
  he_pct REAL,
  circuit TEXT,
  avg_depth_m REAL,
  source_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  t_s REAL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS undecoded_payloads (
  id INTEGER PRIMARY KEY,
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  container TEXT NOT NULL,         -- 'fit_mesg' | 'pnf_record'
  type_key TEXT NOT NULL,          -- FIT global mesg number | PNF record type '0x51'
  seq INTEGER NOT NULL,
  payload BLOB,                    -- raw 32-byte PNF record; NULL for FIT
  fields_json TEXT                 -- FIT field-number -> value dump
);

CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY,
  a_source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  b_source_dive_id INTEGER NOT NULL REFERENCES source_dives(id),
  clock_offset_s INTEGER NOT NULL, -- b's wall clock minus this = a's clock (0 on a shared clock)
  residual_skew_s REAL,            -- seconds to add to b, after the offset, to line up with a
  xcorr_score REAL,
  duration_delta_s REAL,
  max_depth_delta_m REAL,
  method TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'auto',  -- 'auto' | 'confirmed' | 'rejected'
  matched_at TEXT NOT NULL,
  UNIQUE (a_source_dive_id, b_source_dive_id)
);

CREATE TABLE IF NOT EXISTS dives (
  id INTEGER PRIMARY KEY,
  dive_number INTEGER,             -- chronological, non-test dives only
  start_time_utc TEXT NOT NULL,
  utc_offset_min INTEGER,
  duration_s REAL,
  max_depth_m REAL,
  is_test INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dive_members (
  dive_id INTEGER NOT NULL REFERENCES dives(id),
  source_dive_id INTEGER NOT NULL REFERENCES source_dives(id) UNIQUE,
  offset_s REAL NOT NULL DEFAULT 0, -- this log's t_s=0, in seconds after the dive's start
  PRIMARY KEY (dive_id, source_dive_id)
);

CREATE INDEX IF NOT EXISTS idx_events_dive ON events(source_dive_id);
CREATE INDEX IF NOT EXISTS idx_undecoded_dive ON undecoded_payloads(source_dive_id);
CREATE INDEX IF NOT EXISTS idx_source_dives_start ON source_dives(start_time_utc);

CREATE VIEW IF NOT EXISTS v_dive_summary AS
SELECT d.id, d.dive_number, d.start_time_utc, d.utc_offset_min, d.duration_s,
       d.max_depth_m, d.is_test,
       group_concat(sd.source) AS sources,
       group_concat(sd.id) AS source_dive_ids
FROM dives d
JOIN dive_members dm ON dm.dive_id = d.id
JOIN source_dives sd ON sd.id = dm.source_dive_id
GROUP BY d.id;

CREATE VIEW IF NOT EXISTS v_samples_unified AS
SELECT dm.dive_id, s.source_dive_id, 'garmin' AS source, s.t_s,
       s.depth_m, s.temp_c, s.tts_s / 60.0 AS tts_min,
       s.next_stop_depth_m AS stop_depth_m, s.cns_pct
FROM garmin_samples s JOIN dive_members dm ON dm.source_dive_id = s.source_dive_id
UNION ALL
SELECT dm.dive_id, s.source_dive_id, 'shearwater' AS source, s.t_s,
       s.depth_m, s.temp_c, s.tts_min,
       s.stop_depth_m, s.cns_pct
FROM shearwater_samples s JOIN dive_members dm ON dm.source_dive_id = s.source_dive_id;
"""
