"""The app's backend, without a window."""

import sqlite3
import time

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
