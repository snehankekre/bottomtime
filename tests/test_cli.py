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
# The shape pnf.decode() returns: the model is a name, the serial an integer.
SHEARWATER_HEADER = {
    "pnf": {"computer_model": "Perdix 2", "computer_model_id": 11,
            "computer_firmware": 100, "computer_serial": 0xA823C228},
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
            "INSERT INTO source_dives (id, source, artifact_id, source_key, device,"
            " content_hash, decoder_version, start_time_utc, start_time_local,"
            " utc_offset_min, duration_s, max_depth_m, sample_interval_ms, mode,"
            " header_json) VALUES (1,'garmin',1,'act1','garmin:12345','h1','garmin-fit-sdk 21',"
            " '2026-07-06 03:47:01','2026-07-06 11:47:01',480,120,20.0,1000,"
            " 'multi_gas_diving',?)",
            (json.dumps(GARMIN_HEADER),),
        )
        con.execute(
            "INSERT INTO source_dives (id, source, artifact_id, source_key, device,"
            " content_hash, decoder_version, start_time_utc, start_time_local,"
            " utc_offset_min, duration_s, max_depth_m, sample_interval_ms, mode,"
            " header_json) VALUES (2,'shearwater',1,'A823C228#1','shearwater:A823C228','h2','pnf 0.2.0',"
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
            "INSERT INTO matches (a_source_dive_id, b_source_dive_id,"
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
        # The Perdix started 189 s before the Garmin and its clock runs 190 s
        # behind once the zone is removed, so it lines up 1 s after the start.
        con.executemany(
            "INSERT INTO dive_members (dive_id, source_dive_id, offset_s) VALUES (?,?,?)",
            [(1, 1, 0.0), (1, 2, 1.0)],
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
    assert data["sources"]["garmin"][0]["samples"]["depth_m"] == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert data["matches"][0]["clock_offset_s"] == 28800


def test_export_csv_stdout(store, capsys):
    assert cli.main(["--db", str(store), "export", "1"]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    header = rows[0]
    assert header[:5] == ["source", "source_dive_id", "computer", "dive_time_s", "t_s"]
    assert "gf99" in header
    body = rows[1:]
    assert len(body) == 8  # 5 garmin + 3 shearwater, never merged
    assert {r[0] for r in body} == {"garmin", "shearwater"}


def test_old_schema_asks_for_a_rebuild(store):
    con = store_db.connect(store)
    with con:
        con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    con.close()
    with pytest.raises(SystemExit, match="bottomtime rebuild"):
        store_db.connect(store)


def test_aligned_series_uses_stored_member_offsets(store):
    dive = load_dive(store, 1)
    series = {s["source"]: s for s in aligned_series(dive)}
    assert series["garmin"]["x"][0] == 0.0
    assert series["shearwater"]["x"][0] == pytest.approx(1.0)
    assert series["shearwater"]["label"] == "Perdix 2"


def test_dive_summaries(store):
    from bottomtime.api import dive_summaries

    rows = {r["id"]: r for r in dive_summaries(store)}
    assert rows[1]["number"] == 1 and rows[1]["gf99_max"] == 12.0
    assert rows[1]["gf99_surf"] == 12.0  # last 60 samples
    assert rows[2]["is_test"] is True


def test_export_zip_holds_every_dive(store):
    import zipfile

    from bottomtime.app import export_zip

    zf = zipfile.ZipFile(io.BytesIO(export_zip(store)))
    names = zf.namelist()
    assert "dives.csv" in names
    assert "dives/2026-07-06-1-1.csv" in names
    rows = list(csv.reader(io.StringIO(zf.read("dives/2026-07-06-1-1.csv").decode())))
    assert len(rows) == 1 + 8  # header + 5 garmin + 3 shearwater samples


def test_browser_mode_requires_the_token(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    import webbrowser

    from bottomtime import app

    urls = []
    real_open = webbrowser.open
    webbrowser.open = urls.append
    try:
        threading.Thread(target=app._serve, args=(app.Api(tmp_path),), daemon=True).start()
        while not urls:
            pass
    finally:
        webbrowser.open = real_open
    base, token = urls[0].split("/#")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(urllib.request.Request(base + "/api/overview", data=b"[]"))
    req = urllib.request.Request(base + "/api/overview", data=b"[]", headers={"X-Token": token})
    assert json.loads(urllib.request.urlopen(req).read())["dives"] == []
