"""Contract test for the pnf decoder dependency.

The PNF decoder lives in the standalone `pnf` package (with its own full test
suite). bottomtime depends on it and relies on a specific surface: the
`decode` entry point, the `COMPUTER_MODELS` table, and a set of sample fields
the Shearwater ingest inserts into `shearwater_samples`. This test pins that
contract so a breaking pnf upgrade fails here rather than at ingest time.
"""

import pnf


def test_decode_entry_points_exist():
    assert callable(pnf.decode)
    assert callable(pnf.decompress)
    assert 11 in pnf.COMPUTER_MODELS  # Perdix 2, surfaced by api.computer_info


def test_sample_has_fields_ingest_inserts():
    # Every column bottomtime writes to shearwater_samples from a decoded
    # sample (see ingest/shearwater_db.py).
    fields = pnf.Sample.__dataclass_fields__
    for name in (
        "t_s", "depth_m", "temp_c", "stop_depth_m", "stop_or_ndl_min", "tts_min",
        "in_deco", "avg_ppo2", "o2_pct", "he_pct", "setpoint", "cns_pct",
        "gf99", "ceiling_m", "safe_ascent_depth_m", "battery_pct", "sac",
        "sensor1_raw", "sensor2_raw", "sensor3_raw",
        "sensor1_ppo2", "sensor2_ppo2", "sensor3_ppo2",
        "battery_v", "status_flags", "solenoid_fired_count",
        "tank0_psi", "tank1_psi", "gas_time_min", "extra",
    ):
        assert name in fields, f"pnf.Sample lost field {name!r}"


def test_dive_shape_ingest_reads():
    for attr in ("header", "samples", "gases", "events", "unknown_records",
                 "sample_interval_ms"):
        assert attr in pnf.Dive.__dataclass_fields__, f"pnf.Dive lost {attr!r}"
    for attr in ("index", "o2_pct", "he_pct", "diluent", "enabled"):
        assert attr in pnf.Gas.__dataclass_fields__, f"pnf.Gas lost {attr!r}"
