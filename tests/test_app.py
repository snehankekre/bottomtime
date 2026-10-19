"""The app's backend, without a window."""

import io
import json
import sqlite3
import time

import pytest

from bottomtime import app
from bottomtime.store import db as store_db


def _wait(api):
    while api.progress()["busy"]:
        time.sleep(0.05)
    return api.progress()


def test_settings_round_trip(tmp_path):
    api = app.Api(tmp_path)
    assert api.settings()["units"] == "metric"
    api.set_setting("units", "imperial")
    assert app.Api(tmp_path).settings()["units"] == "imperial"
    assert "error" in api.set_setting("no_such_setting", 1)


def test_a_store_from_an_older_version_rebuilds_when_the_app_opens(tmp_path):
    con = store_db.connect(tmp_path / "dives.db")
    with con:
        con.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    con.close()
    api = app.Api(tmp_path)
    assert api.overview()["dives"] == []  # readable while outdated
    assert api.startup() == {"started": True}
    assert _wait(api)["error"] is None
    store_db.connect(tmp_path / "dives.db").close()  # current schema now


def test_errors_are_said_plainly():
    assert "Shearwater Cloud is syncing" in app.friendly("Reading Shearwater Cloud", sqlite3.OperationalError("locked"))
    assert app.friendly("Adding files", ValueError("3 file(s) couldn't be read.")) == "3 file(s) couldn't be read."
    assert app.friendly("Adding files", KeyError("x")) == "Something went wrong while adding files."


def test_delete_everything_leaves_an_empty_store(tmp_path):
    api = app.Api(tmp_path)
    (tmp_path / "archive" / "fit").mkdir(parents=True)
    (tmp_path / "archive" / "fit" / "a.fit").write_bytes(b"x")
    api.set_setting("units", "imperial")
    assert api.delete_everything() == {"status": "ok"}
    assert not (tmp_path / "archive").exists()
    assert api.settings()["units"] == "metric"
    assert api.overview()["dives"] == []


def test_a_rejected_garmin_sign_in_signs_out(tmp_path, monkeypatch):
    garminconnect = pytest.importorskip("garminconnect")

    class Rejected(garminconnect.Garmin):
        def login(self, *args, **kwargs):
            raise garminconnect.GarminConnectAuthenticationError("401 Unauthorized")

    monkeypatch.setattr(garminconnect, "Garmin", Rejected)
    api = app.Api(tmp_path)
    (tmp_path / "garmin").mkdir()
    (tmp_path / "garmin" / "garmin_tokens.json").write_text("{}")
    assert api.overview()["garmin_signed_in"]
    api.garmin_sync()
    assert "sign in again" in _wait(api)["error"]
    assert not api.overview()["garmin_signed_in"]


def test_versions_sort_with_test_builds_before_their_release():
    assert app.version_key("0.8.0b2") < app.version_key("v0.8.0") < app.version_key("0.8.1b1")
    assert app.version_key("0.8.0b1") < app.version_key("0.8.0b2")


def test_only_test_builds_hear_about_test_builds(tmp_path, monkeypatch):
    releases = [
        {"tag_name": "v0.9.0b1", "prerelease": True, "html_url": "b", "assets": []},
        {"tag_name": "v0.8.0", "prerelease": False, "html_url": "r", "assets": []},
    ]

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

    monkeypatch.setattr(app.urllib.request, "urlopen", lambda *a, **k: Reply(json.dumps(releases).encode()))
    api = app.Api(tmp_path)
    monkeypatch.setattr(app, "__version__", "0.7.0")
    assert api.check_for_update()["latest"] == "0.8.0"
    monkeypatch.setattr(app, "__version__", "0.8.0b2")
    assert api.check_for_update()["latest"] == "0.9.0b1"
    monkeypatch.setattr(app, "__version__", "0.9.0")
    assert api.check_for_update() == {}
    # Run from source, so there's no app to replace and nothing downloads.
    assert api.update_status() == {"ready": False, "error": None}
