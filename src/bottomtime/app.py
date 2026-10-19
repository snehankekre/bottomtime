"""Desktop app: a window over the dive store, for divers who don't use a terminal.

    bottomtime app             # native window (pip install 'bottomtime[app]')
    bottomtime app --browser   # the same UI in a browser tab

The UI is one HTML file, app_ui/index.html. It calls the public methods of
Api, through pywebview's bridge in the window or POST /api/<method> in the
browser. The store, its archive, the Garmin sign-in and the settings live in
the app's data folder, so the diver never needs to know where anything is.
"""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .api import compare_logs, computers, dive_summaries, load_dive, write_dive_csv
from .store import db as store_db

UI = Path(__file__).parent / "app_ui" / "index.html"
REPO = "https://github.com/snehankekre/bottomtime"
RELEASES_API = "https://api.github.com/repos/snehankekre/bottomtime/releases?per_page=20"

# Shearwater Cloud's desktop app keeps its live database in Unity's
# persistentDataPath, under users/<account>/dive_data.db ('loadinguser' is a
# placeholder before sign-in). On Windows, Unity puts that folder in LocalLow,
# named after the company and product in the app's app.info.
if sys.platform == "win32":
    CLOUD_USERS = Path.home() / "AppData/LocalLow/Shearwater Research/Shearwater/users"
else:
    CLOUD_USERS = (
        Path.home()
        / "Library/Containers/research.shearwater.cloud/Data/Library"
        / "Application Support/research.shearwater.cloud/users"
    )

DEFAULT_SETTINGS = {
    "units": "metric",          # 'metric' | 'imperial', display only
    "check_on_open": True,      # read Cloud and Garmin when the app opens
    "check_updates": True,      # ask GitHub whether a newer release is out
    "tour_list": False,         # the first-run tour of the dive list was shown
    "tour_dive": False,         # the first-run tour of a dive was shown
    "garmin_account": None,     # the name Garmin shows for the signed-in account
    "charts": ["gf99", "ppo2"], # the charts shown under depth on a dive
}


def data_dir() -> Path:
    if sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/bottomtime"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "bottomtime"
    else:
        path = Path.home() / ".bottomtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_key(v: str) -> tuple:
    """Sortable form of a version such as 0.8.0 or 0.8.0b2, where 0.8.0b2
    comes before 0.8.0."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:b(\d+))?", v)
    if not m:
        return (0,)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4]) if m[4] else 10**9)


def update_asset() -> str | None:
    """The release file this copy of the app updates from."""
    if sys.platform == "win32":
        return "bottomtime-windows.zip"
    if sys.platform == "darwin":
        # An Intel build reports x86_64 even under Rosetta, so it stays Intel.
        return "bottomtime-mac.zip" if platform.machine() == "arm64" else "bottomtime-mac-intel.zip"
    return None


def app_location() -> Path | None:
    """The .app or folder this copy runs from, if it's a built app that can
    replace itself there."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    place = exe.parents[2] if sys.platform == "darwin" else exe.parent
    # macOS runs an app it hasn't moved out of Downloads from a read-only copy.
    if sys.platform == "darwin" and (place.suffix != ".app" or "AppTranslocation" in str(place)):
        return None
    return place if os.access(place, os.W_OK) and os.access(place.parent, os.W_OK) else None


def find_shearwater_cloud() -> Path | None:
    dbs = [p for p in CLOUD_USERS.glob("*/dive_data.db") if p.parent.name != "loadinguser"]
    return max(dbs, key=lambda p: p.stat().st_mtime, default=None)


def friendly(task: str, e: BaseException) -> str:
    """What to tell a diver when something fails. The details go in the
    problem report, not here."""
    name, text = type(e).__name__, str(e)
    if "Garmin" in name and ("Authentication" in name or "401" in text):
        return "Garmin didn't accept the saved sign-in. Click Garmin Connect and sign in again."
    if "Garmin" in name or "curl" in text.lower() or isinstance(e, (ConnectionError, TimeoutError)):
        return f"{task} needs Garmin Connect, and bottomtime couldn't reach it. Check your internet connection and try again."
    if isinstance(e, sqlite3.OperationalError):
        return f"{task} couldn't read a database. If Shearwater Cloud is syncing, wait for it to finish and try again."
    if isinstance(e, ValueError):
        return text
    return f"Something went wrong while {task[0].lower() + task[1:]}."


class Api:
    """Everything the UI can ask for. Attributes are private so pywebview
    exposes only the methods."""

    def __init__(self, home: Path):
        self._home = home
        self._db = home / "dives.db"
        self._archive = home / "archive"
        self._garmin_tokens = home / "garmin"
        self._settings_path = home / "settings.json"
        self._window = None
        self._mfa = None
        self._lock = threading.Lock()
        self._job = {"busy": False, "message": "", "error": None, "detail": None}
        self._update = {"asset": None, "staged": None, "error": None}
        try:
            store_db.connect(self._db).close()
            self._outdated = False
        except store_db.OldStore:
            self._outdated = True

    # ---- settings ----

    def settings(self) -> dict:
        try:
            saved = json.loads(self._settings_path.read_text())
        except (OSError, ValueError):
            saved = {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}

    def set_setting(self, key: str, value) -> dict:
        if key not in DEFAULT_SETTINGS:
            return {"error": f"unknown setting {key}"}
        settings = {**self.settings(), key: value}
        self._settings_path.write_text(json.dumps(settings, indent=2))
        return settings

    # ---- reading ----

    def overview(self) -> dict:
        cloud = find_shearwater_cloud()
        if self._outdated:
            dives, computers = [], 0
        else:
            dives = dive_summaries(self._db)
            con = sqlite3.connect(self._db)
            computers = con.execute(
                "SELECT count(DISTINCT device) FROM source_dives WHERE device NOT LIKE '%:None'"
            ).fetchone()[0]
            con.close()
        return {
            "dives": dives,
            "computers": computers,
            "shearwater_cloud": str(cloud) if cloud else None,
            "shearwater_account": cloud.parent.name if cloud else None,
            "garmin_signed_in": self._signed_in(),
            "data_dir": str(self._home),
            "native": self._window is not None,
            "version": __version__,
            "os": sys.platform,
            "settings": self.settings(),
        }

    def dive(self, dive_id: int) -> dict:
        return dive_chart(self._db, dive_id)

    def progress(self) -> dict:
        return dict(self._job)

    def startup(self) -> dict:
        """Called once when the window opens: rebuild a store an older version
        left behind, or look for new dives if the diver asked for that."""
        if self._outdated:
            return self._start("Updating your dives for this version of bottomtime", self._rebuild)
        # Only once the diver has added dives themselves, so a first launch
        # never reads another app's data before they asked for it.
        con = sqlite3.connect(self._db)
        has_dives = con.execute("SELECT 1 FROM source_dives LIMIT 1").fetchone() is not None
        con.close()
        if self.settings()["check_on_open"] and has_dives and (find_shearwater_cloud() or self._signed_in()):
            return self._start("Looking for new dives", self._check_all)
        return {"idle": True}

    def check_for_update(self) -> dict:
        """Say whether a newer version is out and, when this copy can replace
        itself, start downloading it."""
        if not self.settings()["check_updates"]:
            return {}
        try:
            headers = {"Accept": "application/vnd.github+json"}
            # CI machines share addresses, so without a token GitHub limits them.
            if os.environ.get("GITHUB_TOKEN"):
                headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
            req = urllib.request.Request(RELEASES_API, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                releases = json.load(r)
        except Exception:  # noqa: BLE001 - offline is not an error worth showing
            return {}
        # BOTTOMTIME_TEST_VERSION lets a test run an old copy's update path.
        current = os.environ.get("BOTTOMTIME_TEST_VERSION") or __version__
        # A test build hears about test builds. Everyone else hears only about releases.
        testing = "b" in current
        candidates = [r for r in releases if not r.get("draft") and (testing or not r.get("prerelease"))]
        # Prefer the newest release that has this computer's download. A release
        # can be out for a few minutes before CI attaches its Windows zip.
        candidates = [r for r in candidates if any(a["name"] == update_asset() for a in r.get("assets", []))] or candidates
        latest = max(candidates, key=lambda r: version_key(r["tag_name"]), default=None)
        if not latest or version_key(latest["tag_name"]) <= version_key(current):
            return {}
        tag = latest["tag_name"].lstrip("v")
        asset = next((a for a in latest.get("assets", []) if a["name"] == update_asset()), None)
        place = app_location()
        if asset and place and not self._update["asset"]:
            self._update["asset"] = asset
            threading.Thread(target=self._stage_update, args=(asset["browser_download_url"], place), daemon=True).start()
        return {"latest": tag, "url": latest.get("html_url"), "downloading": bool(self._update["asset"])}

    def update_status(self) -> dict:
        return {"ready": self._update["staged"] is not None, "error": self._update["error"]}

    def _stage_update(self, url: str, place: Path) -> None:
        """Download and unpack the new version next to this one. It replaces
        this one when the app quits or restarts, never while it runs."""
        work = Path(tempfile.mkdtemp(prefix=".bottomtime-update-", dir=place.parent))
        try:
            archive = work / "update.zip"
            with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
            if sys.platform == "darwin":
                subprocess.run(["ditto", "-x", "-k", str(archive), str(work)], check=True)
                # A download cut short or tampered with in transit fails here.
                subprocess.run(["codesign", "--verify", "--deep", "--strict", str(work / "bottomtime.app")],
                               check=True, capture_output=True)
            else:
                with zipfile.ZipFile(archive) as z:
                    z.extractall(work)
                if not (work / "bottomtime" / "bottomtime.exe").is_file():
                    raise ValueError("The update doesn't contain bottomtime.exe.")
            archive.unlink()
            self._update["staged"] = work
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            shutil.rmtree(work, ignore_errors=True)
            self._update["error"] = str(e) or type(e).__name__

    def restart(self) -> dict:
        """Install the downloaded version and open it."""
        if not self._update["staged"] or not app_location():
            return {"error": "There's no downloaded update to install."}
        self.finish_update(relaunch=True)
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {}

    def finish_update(self, relaunch: bool = False) -> None:
        """Swap in the downloaded version, if there is one. Called as the app
        quits, so an update the diver didn't restart for still installs."""
        work, place = self._update["staged"], app_location()
        if not work or not place:
            return
        self._update["staged"] = None
        if sys.platform == "darwin":
            old = work / "old.app"
            os.rename(place, old)
            try:
                os.rename(work / "bottomtime.app", place)
            except OSError:
                os.rename(old, place)
                raise
            shutil.rmtree(work, ignore_errors=True)
            if relaunch:
                subprocess.Popen(["open", "-n", str(place)])
            return
        # Windows won't replace a running program's files, so a script waits
        # for this copy to exit, then moves the new files in. It touches only
        # bottomtime.exe and _internal, never anything else in the folder.
        pid, new = os.getpid(), work / "bottomtime"
        script = place.parent / f".bottomtime-update-{pid}.cmd"
        script.write_text(
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)\r\n'
            f'rmdir /s /q "{place}\\_internal"\r\n'
            f'del /q "{place}\\bottomtime.exe"\r\n'
            f'move "{new}\\_internal" "{place}\\_internal" >nul\r\n'
            f'move "{new}\\bottomtime.exe" "{place}\\bottomtime.exe" >nul\r\n'
            f'rmdir /s /q "{work}"\r\n'
            + (f'start "" "{place}\\bottomtime.exe"\r\n' if relaunch else "")
            + '(goto) 2>nul & del "%~f0"\r\n',
            newline="",
        )
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # ---- importing ----

    def import_shearwater_cloud(self) -> dict:
        path = find_shearwater_cloud()
        if path is None:
            return {"error": "Shearwater Cloud's desktop app wasn't found on this computer."}
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
        except Exception as e:  # noqa: BLE001
            return {"error": _sign_in_error(e)}
        if status == "needs_mfa":
            self._mfa = (garmin, state)
            return {"status": "mfa"}
        return self._signed_in_as(garmin)

    def garmin_mfa(self, code: str) -> dict:
        if not self._mfa:
            return {"error": "Start the Garmin sign-in again."}
        garmin, state = self._mfa
        try:
            garmin.resume_login(state, code.strip())
        except Exception as e:  # noqa: BLE001
            return {"error": "Garmin didn't accept that code. " + _sign_in_error(e)}
        self._mfa = None
        return self._signed_in_as(garmin)

    def garmin_sign_out(self) -> dict:
        shutil.rmtree(self._garmin_tokens, ignore_errors=True)
        self.set_setting("garmin_account", None)
        return {"status": "ok"}

    def garmin_sync(self) -> dict:
        if not self._signed_in():
            return {"error": "Sign in to Garmin Connect first."}
        return self._start("Downloading from Garmin Connect", self._sync_garmin)

    # ---- the data folder ----

    def show_data_folder(self) -> dict:
        if sys.platform == "win32":
            os.startfile(self._home)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(self._home)], check=False)
        return {}

    def rebuild_store(self) -> dict:
        return self._start("Rebuilding your dives from the saved files", self._rebuild)

    def delete_everything(self) -> dict:
        """Forget every dive, every saved original, the Garmin sign-in and the
        settings. Shearwater Cloud and Garmin Connect are not touched."""
        with self._lock:
            if self._job["busy"]:
                return {"error": "Wait for the current task to finish."}
            for name in ("dives.db", "dives.db-wal", "dives.db-shm", "settings.json"):
                (self._home / name).unlink(missing_ok=True)
            shutil.rmtree(self._archive, ignore_errors=True)
            shutil.rmtree(self._garmin_tokens, ignore_errors=True)
            store_db.connect(self._db).close()
            self._outdated = False
        return {"status": "ok"}

    # ---- links out ----

    def open_link(self, which: str) -> dict:
        urls = {"repo": REPO, "releases": f"{REPO}/releases/latest", "issues": f"{REPO}/issues",
                "manual": f"{REPO}/blob/main/docs/manual.md"}
        if which in urls:
            _open(urls[which])
        return {}

    def report_problem(self, message: str = "", detail: str = "") -> dict:
        body = (
            f"**What happened**\n\n{message or '(describe what you were doing)'}\n\n"
            f"**Details**\n\n```\n{detail[-3000:]}\n```\n\n"
            f"bottomtime {__version__}, {platform.platform()}"
        )
        query = urllib.parse.urlencode({"title": message[:80] or "Problem in the app", "body": body})
        _open(f"{REPO}/issues/new?{query}")
        return {}

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

    def _signed_in(self) -> bool:
        return (self._garmin_tokens / "garmin_tokens.json").exists()

    def _signed_in_as(self, garmin) -> dict:
        garmin.client.dump(str(self._garmin_tokens))
        try:
            name = garmin.get_full_name()
        except Exception:  # noqa: BLE001 - the name is only for display
            name = None
        self.set_setting("garmin_account", name)
        return {"status": "ok"}

    def _start(self, label: str, work) -> dict:
        with self._lock:
            if self._job["busy"]:
                return {"error": "Still working on the last request."}
            self._job = {"busy": True, "message": label, "error": None, "detail": None}

        def run():
            try:
                if work != self._rebuild:
                    con = store_db.connect(self._db)
                    try:
                        work(con)
                        self._say("Matching dives across computers")
                        from .match.matcher import run_match

                        run_match(con, verbose=False)
                    finally:
                        con.close()
                else:
                    work()
                self._job = {"busy": False, "message": "Done", "error": None, "detail": None}
            except Exception as e:  # noqa: BLE001
                detail = traceback.format_exc()
                print(detail, file=sys.stderr)
                self._job = {"busy": False, "message": "", "error": friendly(label, e), "detail": detail}

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    def _say(self, message: str) -> None:
        self._job = {**self._job, "message": message}

    def _rebuild(self) -> None:
        from .rebuild import rebuild

        if rebuild(self._db, self._archive, say=lambda _: None):
            raise ValueError(
                "Some saved files couldn't be read, so your dives were left as they were."
            )
        self._outdated = False

    def _check_all(self, con) -> None:
        cloud = find_shearwater_cloud()
        if cloud:
            self._say("Reading Shearwater Cloud")
            self._import_cloud(con, cloud)
        if self._signed_in():
            self._say("Checking Garmin Connect")
            self._sync_garmin(con)

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
        if not fits and not dbs:
            raise ValueError("Choose .fit files from a Garmin, or a .db file exported from Shearwater Cloud.")
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
        total = len(fits) + len(dbs)
        if failed == total:
            raise ValueError(
                f"bottomtime couldn't read {'that file' if total == 1 else 'those files'}. It reads"
                " .fit files from Garmin computers and .db files exported from Shearwater Cloud."
            )
        if failed:
            raise ValueError(f"{failed} of {total} files couldn't be read. The others were added.")

    def _sync_garmin(self, con) -> None:
        from garminconnect import Garmin

        from .ingest.archive import archive_file
        from .ingest.garmin_fit import ingest_fit_file

        garmin = Garmin()
        try:
            garmin.login(str(self._garmin_tokens))
        except Exception as e:
            # A rejected token is useless; dropping it turns the button
            # back into "Garmin Connect" so the diver can sign in again.
            if "Authentication" in type(e).__name__ or "401" in str(e):
                self.garmin_sign_out()
            raise
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
            raise ValueError(f"{stats['failed']} dive(s) couldn't be read. The rest were added.")


def _sign_in_error(e: Exception) -> str:
    text = str(e)
    if "401" in text or "credential" in text.lower() or "password" in text.lower():
        return "Check the email and password, then try again."
    if "429" in text or "Too Many" in text:
        return "Garmin is limiting sign-ins right now. Wait a few minutes and try again."
    return "Garmin Connect couldn't be reached, or refused the sign-in. Check your internet connection and try again."


def _open(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


# Channels the charts and tooltip show, per source: name -> column.
CHANNELS = {
    "garmin": {"depth": "depth_m", "ppo2": "po2", "temp": "temp_c", "stop": "next_stop_depth_m",
               "tts": "tts_s", "ndl": "ndl_s", "cns": "cns_pct", "n2": "n2_load", "hr": "heart_rate"},
    "shearwater": {"depth": "depth_m", "ceiling": "ceiling_m", "gf99": "gf99", "ppo2": "avg_ppo2",
                   "temp": "temp_c", "stop": "stop_depth_m", "tts": "tts_min", "at5": "at_plus_five_min",
                   "ndl": "ndl_min", "cns": "cns_pct"},
}


def dive_chart(db: Path, dive_id: int) -> dict:
    dive = load_dive(db, dive_id=dive_id)
    # One entry per computer. A computer with several logs (a Shearwater ends
    # a dive at each surface interval) gets one series, with an empty point
    # between its logs so the lines break where it wasn't logging.
    logs = []
    for source, group in computers(dive):
        channels = {
            name: column
            for name, column in CHANNELS.get(source, {}).items()
            if any(v is not None for log in group for v in log["samples"].get(column) or [])
        }
        series = {"t": [], **{name: [] for name in channels}}
        for log in group:
            samples = log["samples"]
            t = [round(log["offset_s"] + x, 1) for x in samples["t_s"]]
            if series["t"] and t:
                series["t"].append(round((series["t"][-1] + t[0]) / 2, 1))
                for name in channels:
                    series[name].append(None)
            series["t"] += t
            for name, column in channels.items():
                values = samples.get(column) or [None] * len(t)
                if column in ("tts_s", "ndl_s"):
                    values = [None if v is None else v / 60 for v in values]
                series[name] += values
        first = group[0]
        logs.append(
            {
                "source": source,
                "name": next((log["name"] for log in group if log["name"]), None),
                "computer": first["computer"]["model"] or source,
                "serial": first["computer"]["serial"],
                "logs": len(group),
                "start_local": first["start_time_local"],
                "duration_s": sum(log["duration_s"] or 0 for log in group),
                "max_depth_m": max(log["max_depth_m"] or 0 for log in group),
                "mode": first["mode"],
                "gases": [
                    f"{g['o2_pct']:g}/{g['he_pct']:g}"
                    for g in first["gases"]
                    if g["enabled"] and (g["o2_pct"] or g["he_pct"])
                ],
                "series": series,
            }
        )
    return {
        "id": dive["id"],
        "number": dive["dive_number"],
        "name": next((lg["name"] for lg in logs if lg["source"] == "shearwater" and lg["name"]), None)
        or next((lg["name"] for lg in logs if lg["name"]), None),
        "start_time_utc": dive["start_time_utc"],
        "utc_offset_min": dive["utc_offset_min"],
        "duration_s": dive["duration_s"],
        "max_depth_m": dive["max_depth_m"],
        "logs": logs,
        "matches": dive["matches"],
        "compare": compare_logs(dive),
    }


def dive_filename(dive: dict) -> str:
    label = f"dive-{dive['dive_number']}" if dive["dive_number"] else "pool-or-test-dive"
    return f"{dive['start_time_utc'][:10]}-{label}.csv"


def export_zip(db: Path) -> bytes:
    """dives.csv with one row per dive, plus every dive's samples as its own
    CSV, so the data stays readable without bottomtime."""
    import csv

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        summaries = dive_summaries(db)
        text = io.StringIO()
        fields = [k for k in summaries[0] if k != "id"] if summaries else []
        writer = csv.writer(text)
        writer.writerow(["file"] + fields)
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
        _serve(api)
    else:
        import webview

        api._window = webview.create_window(
            "bottomtime", url=str(UI), js_api=api, width=1280, height=860, min_size=(900, 600)
        )
        webview.start()
    api.finish_update()
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
