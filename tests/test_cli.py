import csv
import io
import json

import pytest

from bottomtime import cli
from bottomtime.api import computer_info, load_dive
from bottomtime.plot import aligned_series
from bottomtime.store import db as store_db

GARMIN_HEADER = {
    "file_id_mesgs": [{"garmin_product": "descent_mk3", "serial_number": 12345}],
    "device_info_mesgs": [{"software_version": 25.21}],
}
SHEARWATER_HEADER = {
    "pnf": {"computer_model": 11, "computer_firmware": 100,
            "computer_serial": 0xA823C228},
    "dive_details": {"SerialNumber": "A823C228"},
}


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "dives.db"
    con = store_db.connect(db_path)
    with con:
        con.execute(
            "INSERT INTO raw_artifacts (id, kind, sha256, size_bytes, original_path,"
            " archive_path, imported_at) VALUES (1,'fit','x',1,'a','b','now')"
        )
        con.execute(
            "INSERT INTO source_dives (id, source, artifact_id, source_key,"
            " content_hash, decoder_version, start_time_utc, start_time_local,"
            " utc_offset_min, duration_s, max_depth_m, sample_interval_ms, mode,"
            " header_json) VALUES (1,'garmin',1,'act1','h1',1,"
            " '2026-07-06 03:47:01','2026-07-06 11:47:01',480,120,20.0,1000,"
            " 'multi_gas_diving',?)",
            (json.dumps(GARMIN_HEADER),),
        )
        con.execute(
            "INSERT INTO source_dives (id, source, artifact_id, source_key,"
            " content_hash, decoder_version, start_time_utc, start_time_local,"
            " utc_offset_min, duration_s, max_depth_m, sample_interval_ms, mode,"
            " header_json) VALUES (2,'shearwater',1,'A823C228#1','h2',2,"
            " '2026-07-06 03:43:52','2026-07-06 11:43:52',480,110,20.2,10000,"
            " 'oc_tec',?)",
            (json.dumps(SHEARWATER_HEADER),),
        )
        con.executemany(
            "INSERT INTO garmin_samples (source_dive_id, t_s, depth_m) VALUES (1,?,?)",
            [(t, t * 0.5) for t in range(5)],
        )
        con.executemany(
            "INSERT INTO shearwater_samples (source_dive_id, t_s, depth_m, gf99)"
            " VALUES (2,?,?,?)",
            [(t * 10.0, t * 5.0, 10.0 + t) for t in range(3)],
        )
        con.execute(
            "INSERT INTO gases (source_dive_id, gas_index, o2_pct, he_pct, circuit,"
            " enabled) VALUES (2, 0, 21, 35, 'oc', 1)"
        )
        con.execute(
            "INSERT INTO events (source_dive_id, t_s, kind, payload_json)"
            " VALUES (2, 0, 'pnf_info_event', '{}')"
        )
        con.execute(
            "INSERT INTO matches (garmin_source_dive_id, shearwater_source_dive_id,"
            " clock_offset_s, residual_skew_s, xcorr_score, duration_delta_s,"
            " max_depth_delta_m, method, status, matched_at)"
            " VALUES (1, 2, 28800, 190.0, 0.999, 10, 0.2, 'test', 'auto', 'now')"
        )
        con.execute(
            "INSERT INTO dives (id, dive_number, start_time_utc, utc_offset_min,"
            " duration_s, max_depth_m, is_test) VALUES"
            " (1, 1, '2026-07-06 03:47:01', 480, 120, 20.2, 0)"
        )
        con.execute(
            "INSERT INTO dives (id, dive_number, start_time_utc, utc_offset_min,"
            " duration_s, max_depth_m, is_test) VALUES"
            " (2, NULL, '2026-07-06 05:00:00', 480, 30, 1.0, 1)"
        )
        con.executemany(
            "INSERT INTO dive_members (dive_id, source_dive_id) VALUES (?,?)",
            [(1, 1), (1, 2)],
        )
    con.close()
    return db_path


def test_computer_info():
    g = computer_info("garmin", GARMIN_HEADER)
    assert g == {"model": "Descent Mk3", "firmware": 25.21, "serial": 12345}
    s = computer_info("shearwater", SHEARWATER_HEADER)
    assert s == {"model": "Perdix 2", "firmware": "v100", "serial": "A823C228"}


def test_list(store, capsys):
    assert cli.main(["--db", str(store), "list"]) == 0
    out = capsys.readouterr().out
    assert "Descent Mk3 + Perdix 2" in out
    assert "2026-07-06 11:47" in out  # local time
    assert "oc_tec" in out  # shearwater mode preferred
    assert "T" not in out.splitlines()[0]  # header, then only dive 1


def test_show(store, capsys):
    assert cli.main(["--db", str(store), "show", "1"]) == 0
    out = capsys.readouterr().out
    assert "dive 1" in out
    assert "Descent Mk3 25.21" in out
    assert "Perdix 2 v100" in out
    assert "gases: 21/35 oc" in out
    assert "clock offset +28800 s" in out
    assert "gf99" in out  # channel coverage


def test_show_missing_dive(store, capsys):
    assert cli.main(["--db", str(store), "show", "99"]) == 1
    assert "no dive with number 99" in capsys.readouterr().err


def test_export_json(store, tmp_path, capsys):
    out_file = tmp_path / "d1.json"
    assert cli.main(
        ["--db", str(store), "export", "1", "--format", "json", "-o", str(out_file)]
    ) == 0
    data = json.loads(out_file.read_text())
    assert sorted(data["sources"]) == ["garmin", "shearwater"]
    assert data["sources"]["garmin"]["samples"]["depth_m"] == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert data["matches"][0]["clock_offset_s"] == 28800


def test_export_csv_stdout(store, capsys):
    assert cli.main(["--db", str(store), "export", "1"]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    header = rows[0]
    assert header[:3] == ["source", "source_dive_id", "t_s"]
    assert "gf99" in header
    body = rows[1:]
    assert len(body) == 8  # 5 garmin + 3 shearwater, never merged
    assert {r[0] for r in body} == {"garmin", "shearwater"}


def test_aligned_series_applies_clock_offset_and_skew(store):
    dive = load_dive(store, 1)
    series = {s["source"]: s for s in aligned_series(dive)}
    assert series["garmin"]["x"][0] == 0.0
    # shearwater start_utc is 189 s before the dive start; +190 s skew -> +1 s
    assert series["shearwater"]["x"][0] == pytest.approx(1.0)
    assert series["shearwater"]["label"] == "Perdix 2"
