"""Desktop app: a window over the dive store, for divers who don't use a terminal.

    bottomtime app             # native window (pip install 'bottomtime[app]')
    bottomtime app --browser   # the same UI in a browser tab

The UI is one HTML file, app_ui/index.html. It calls the public methods of
Api, through pywebview's bridge in the window or POST /api/<method> in the
browser. The store and its archive live in the app's data folder, so the app
never needs the diver to know where anything is.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import dive_summaries, load_dive, source_logs, write_dive_csv
from .store import db as store_db

UI = Path(__file__).parent / "app_ui" / "index.html"

# Shearwater Cloud's desktop app keeps its live database here on a Mac, one
# folder per signed-in account ('loadinguser' is a placeholder before sign-in).
CLOUD_USERS = (
    Path.home()
    / "Library/Containers/research.shearwater.cloud/Data/Library"
    / "Application Support/research.shearwater.cloud/users"
)


def data_dir() -> Path:
    if sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/bottomtime"
    else:
        path = Path.home() / ".bottomtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_shearwater_cloud() -> Path | None:
    dbs = [p for p in CLOUD_USERS.glob("*/dive_data.db") if p.parent.name != "loadinguser"]
    return max(dbs, key=lambda p: p.stat().st_mtime, default=None)


class Api:
    """Everything the UI can ask for. Attributes are private so pywebview
    exposes only the methods."""

    def __init__(self, home: Path):
        self._home = home
        self._db = home / "dives.db"
        self._archive = home / "archive"
        self._garmin_tokens = home / "garmin"
        self._window = None
        self._mfa = None
        self._lock = threading.Lock()
        self._job = {"busy": False, "message": "", "error": None}
        store_db.connect(self._db).close()

    # ---- reading ----

    def overview(self) -> dict:
        cloud = find_shearwater_cloud()
        con = sqlite3.connect(self._db)
        computers = con.execute(
            "SELECT count(DISTINCT device) FROM source_dives WHERE device NOT LIKE '%:None'"
        ).fetchone()[0]
        con.close()
        return {
            "dives": dive_summaries(self._db),
            "computers": computers,
            "shearwater_cloud": str(cloud) if cloud else None,
            "garmin_signed_in": (self._garmin_tokens / "garmin_tokens.json").exists(),
            "data_dir": str(self._home),
            "native": self._window is not None,
        }

    def dive(self, dive_id: int) -> dict:
        return dive_chart(self._db, dive_id)

    def progress(self) -> dict:
        return dict(self._job)

    # ---- importing ----

    def import_shearwater_cloud(self) -> dict:
        path = find_shearwater_cloud()
        if path is None:
            return {"error": "Shearwater Cloud's database was not found on this Mac."}
        return self._start("Reading Shearwater Cloud", lambda con: self._import_cloud(con, path))

    def choose_files(self) -> dict:
        import webview

        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=("Dive logs (*.fit;*.db)", "All files (*.*)"),
        )
        if not paths:
            return {"cancelled": True}
        return self._start("Adding files", lambda con: self._import_paths(con, [Path(p) for p in paths]))

    def import_uploads(self, files: list[dict]) -> dict:
        """Browser mode: files arrive as {name, data} with base64 data."""
        tmp = Path(tempfile.mkdtemp(prefix="bottomtime-upload-"))
        paths = []
        for f in files:
            path = tmp / Path(f["name"]).name
            path.write_bytes(base64.b64decode(f["data"]))
            paths.append(path)
        return self._start("Adding files", lambda con: self._import_paths(con, paths))

    def garmin_sign_in(self, email: str, password: str) -> dict:
        from garminconnect import Garmin

        try:
            garmin = Garmin(email=email, password=password, return_on_mfa=True)
            status, state = garmin.login()
        except Exception as e:  # noqa: BLE001 - shown to the diver as-is
            return {"error": f"Garmin sign-in failed: {e}"}
        if status == "needs_mfa":
            self._mfa = (garmin, state)
            return {"status": "mfa"}
        garmin.client.dump(str(self._garmin_tokens))
        return {"status": "ok"}

    def garmin_mfa(self, code: str) -> dict:
        if not self._mfa:
            return {"error": "Start the Garmin sign-in again."}
        garmin, state = self._mfa
        try:
            garmin.resume_login(state, code.strip())
        except Exception as e:  # noqa: BLE001
            return {"error": f"Garmin did not accept that code: {e}"}
        self._mfa = None
        garmin.client.dump(str(self._garmin_tokens))
        return {"status": "ok"}

    def garmin_sign_out(self) -> dict:
        for f in self._garmin_tokens.glob("*"):
            f.unlink()
        return {"status": "ok"}

    def garmin_sync(self) -> dict:
        if not (self._garmin_tokens / "garmin_tokens.json").exists():
            return {"error": "Sign in to Garmin Connect first."}
        return self._start("Downloading from Garmin Connect", self._sync_garmin)

    # ---- exporting ----

    def export_dive(self, dive_id: int) -> dict:
        dive = load_dive(self._db, dive_id=dive_id)
        path = self._save_dialog(dive_filename(dive))
        if not path:
            return {"cancelled": True}
        with open(path, "w", newline="") as fh:
            write_dive_csv(dive, fh)
        return {"path": path}

    def export_all(self) -> dict:
        path = self._save_dialog("bottomtime-export.zip")
        if not path:
            return {"cancelled": True}
        Path(path).write_bytes(export_zip(self._db))
        return {"path": path}

    def _save_dialog(self, name: str) -> str | None:
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name)
        if not chosen:
            return None
        return chosen if isinstance(chosen, str) else chosen[0]

    # ---- jobs ----

    def _start(self, label: str, work) -> dict:
        with self._lock:
            if self._job["busy"]:
                return {"error": "Still working on the last request."}
            self._job = {"busy": True, "message": label, "error": None}

        def run():
            con = store_db.connect(self._db)
            try:
                work(con)
                self._say("Matching dives across computers")
                from .match.matcher import run_match

                run_match(con, verbose=False)
                self._job = {"busy": False, "message": "Done", "error": None}
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._job = {"busy": False, "message": "", "error": str(e)}
            finally:
                con.close()

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    def _say(self, message: str) -> None:
        self._job = {**self._job, "message": message}

    def _import_cloud(self, con, path: Path) -> None:
        from .ingest.shearwater_db import ingest_shearwater_db

        # A consistent copy, safe while Shearwater Cloud is running.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "dive_data.db"
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            dst = sqlite3.connect(snapshot)
            src.backup(dst)
            src.close()
            dst.close()
            self._fail_if(ingest_shearwater_db(con, self._archive, snapshot))

    def _import_paths(self, con, paths: list[Path]) -> None:
        from .ingest.garmin_fit import ingest_fit_file
        from .ingest.shearwater_db import ingest_shearwater_db

        files = []
        for p in paths:
            files += sorted(p.rglob("*")) if p.is_dir() else [p]
        fits = [p for p in files if p.suffix.lower() == ".fit"]
        dbs = [p for p in files if p.suffix.lower() == ".db"]
        failed = 0
        for i, p in enumerate(fits, 1):
            self._say(f"Adding FIT file {i} of {len(fits)}")
            try:
                with con:
                    ingest_fit_file(con, self._archive, p, {})
            except Exception:  # noqa: BLE001
                failed += 1
        for p in dbs:
            self._say(f"Reading {p.name}")
            failed += ingest_shearwater_db(con, self._archive, p)["failed"]
        if not fits and not dbs:
            raise ValueError("Choose .fit files from a Garmin, or a Shearwater Cloud .db export.")
        if failed:
            raise ValueError(f"{failed} file(s) could not be read.")

    def _sync_garmin(self, con) -> None:
        from garminconnect import Garmin

        from .ingest.archive import archive_file
        from .ingest.garmin_fit import ingest_fit_file

        garmin = Garmin()
        garmin.login(str(self._garmin_tokens))
        self._say("Listing your Garmin dives")
        dives = garmin.get_activities_by_date(
            "2000-01-01", date.today().isoformat(), activitytype="diving"
        )
        index = {str(d["activityId"]): d for d in dives}
        # A FIT file added by hand has no activityId, so also skip any
        # activity whose start time is already in the store.
        have = set()
        for key, start in con.execute(
            "SELECT source_key, start_time_utc FROM source_dives WHERE source='garmin'"
        ):
            have |= {key, start}
        # Skip copies of other computers' dives that Garmin's dive log import
        # tool made; bottomtime reads those computers directly.
        todo = [
            d
            for d in dives
            if d.get("manufacturer", "GARMIN") == "GARMIN"
            and not {str(d["activityId"]), d.get("startTimeGMT")} & have
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "dives_index.json"
            index_path.write_text(json.dumps(dives, separators=(",", ":")))
            with con:
                archive_file(con, self._archive, "garmin_index", index_path)
            for i, d in enumerate(todo, 1):
                self._say(f"Downloading Garmin dive {i} of {len(todo)}")
                data = garmin.download_activity(
                    d["activityId"], dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
                )
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    name = next(n for n in zf.namelist() if n.lower().endswith(".fit"))
                    path = Path(tmp) / f"{d['activityId']}.fit"
                    path.write_bytes(zf.read(name))
                with con:
                    ingest_fit_file(con, self._archive, path, index)
                time.sleep(1)  # one download a second, like Garmin Express

    @staticmethod
    def _fail_if(stats: dict) -> None:
        if stats["failed"]:
            raise ValueError(f"{stats['failed']} dive(s) could not be read.")


# Channels the charts and tooltip show, per source: name -> column.
CHANNELS = {
    "garmin": {"depth": "depth_m", "ppo2": "po2", "temp": "temp_c", "stop": "next_stop_depth_m",
               "tts": "tts_s", "cns": "cns_pct"},
    "shearwater": {"depth": "depth_m", "ceiling": "ceiling_m", "gf99": "gf99", "ppo2": "avg_ppo2",
                   "temp": "temp_c", "stop": "stop_depth_m", "tts": "tts_min", "at5": "at_plus_five_min",
                   "ndl": "ndl_min", "cns": "cns_pct"},
}


def dive_chart(db: Path, dive_id: int) -> dict:
    dive = load_dive(db, dive_id=dive_id)
    logs = []
    for source, log in source_logs(dive):
        samples = log["samples"]
        series = {"t": [round(log["offset_s"] + t, 1) for t in samples["t_s"]]}
        for name, column in CHANNELS.get(source, {}).items():
            values = samples.get(column)
            if values and any(v is not None for v in values):
                if column == "tts_s":
                    values = [None if v is None else v / 60 for v in values]
                series[name] = values
        logs.append(
            {
                "source": source,
                "computer": log["computer"]["model"] or source,
                "serial": log["computer"]["serial"],
                "start_local": log["start_time_local"],
                "duration_s": log["duration_s"],
                "max_depth_m": log["max_depth_m"],
                "mode": log["mode"],
                "gases": [
                    f"{g['o2_pct']:g}/{g['he_pct']:g}"
                    for g in log["gases"]
                    if g["enabled"] and (g["o2_pct"] or g["he_pct"])
                ],
                "series": series,
            }
        )
    return {
        "id": dive["id"],
        "number": dive["dive_number"],
        "start_time_utc": dive["start_time_utc"],
        "utc_offset_min": dive["utc_offset_min"],
        "duration_s": dive["duration_s"],
        "max_depth_m": dive["max_depth_m"],
        "logs": logs,
        "matches": dive["matches"],
    }


def dive_filename(dive: dict) -> str:
    label = f"dive-{dive['dive_number']}" if dive["dive_number"] else "pool-or-test-dive"
    return f"{dive['start_time_utc'][:10]}-{label}.csv"


def export_zip(db: Path) -> bytes:
    """dives.csv with one row per dive, plus every dive's samples as its own
    CSV, so the data stays readable without bottomtime."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        summaries = dive_summaries(db)
        text = io.StringIO()
        fields = [k for k in summaries[0] if k != "id"] if summaries else []
        text.write(",".join(["file"] + fields) + "\n")
        import csv

        writer = csv.writer(text)
        for s in summaries:
            name = f"dives/{s['start_time_utc'][:10]}-{s['number'] or 'test'}-{s['id']}.csv"
            writer.writerow([name] + [
                " + ".join(s[k]) if isinstance(s[k], list) else s[k] for k in fields
            ])
            one = io.StringIO()
            write_dive_csv(load_dive(db, dive_id=s["id"]), one)
            zf.writestr(name, one.getvalue())
        zf.writestr("dives.csv", text.getvalue())
    return buf.getvalue()


def run(browser: bool = False, home: Path | None = None) -> int:
    api = Api(home or data_dir())
    if browser:
        return _serve(api)
    import webview

    api._window = webview.create_window(
        "bottomtime", url=str(UI), js_api=api, width=1280, height=860, min_size=(900, 600)
    )
    webview.start()
    return 0


def _serve(api: Api) -> int:
    import webbrowser

    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, kind: str, name: str | None = None):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                return self._send(UI.read_bytes(), "text/html; charset=utf-8")
            # Downloads carry the token in the query string, since a link
            # can't set headers.
            if f"t={token}" not in query:
                return self.send_error(403)
            if path == "/export/all.zip":
                return self._send(export_zip(api._db), "application/zip", "bottomtime-export.zip")
            if path.startswith("/export/dive/"):
                dive = load_dive(api._db, dive_id=int(path.rsplit("/", 1)[1].removesuffix(".csv")))
                text = io.StringIO()
                write_dive_csv(dive, text)
                return self._send(text.getvalue().encode(), "text/csv", dive_filename(dive))
            self.send_error(404)

        def do_POST(self):
            name = self.path.removeprefix("/api/")
            if self.headers.get("X-Token") != token or name.startswith("_") or not hasattr(api, name):
                return self.send_error(403)
            args = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"[]")
            result = getattr(api, name)(*args)
            self._send(json.dumps(result).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#{token}"
    print(f"bottomtime is running at {url}  (Ctrl-C to stop)", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
