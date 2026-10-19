"""Entry point for the packaged app. --browser shows the UI in a browser tab,
which is also how the built app gets tested. --update-test is described below.

--selftest opens the real window on an empty store, waits for the page to
say whether Shearwater Cloud is installed (which needs the page's script and
pywebview's bridge to both work), and exits 0 if it did. CI runs it on
Windows, where nobody can look at the window. The windowed app has no
console, so it also writes what it saw to selftest.txt in the current folder."""

import sys

from bottomtime.app import run

if "--update-test" in sys.argv:
    # CI: with BOTTOMTIME_TEST_VERSION set older than the newest release,
    # download that release, then install it the way quitting does.
    import tempfile
    import time
    from pathlib import Path

    from bottomtime import app

    api = app.Api(Path(tempfile.mkdtemp()))
    found = api.check_for_update()
    for _ in range(180):
        status = api.update_status()
        if status["ready"] or status["error"]:
            break
        time.sleep(1)
    Path("update.txt").write_text(f"{found} {status}\n")
    api.finish_update()
    sys.exit(0 if status["ready"] else 1)

if "--selftest" not in sys.argv:
    sys.exit(run(browser="--browser" in sys.argv))

import tempfile
import time
from pathlib import Path

import webview

from bottomtime import app

home = Path(tempfile.mkdtemp())
api = app.Api(home)
window = api._window = webview.create_window("bottomtime", url=str(app.UI), js_api=api)
seen = ""


def probe():
    global seen
    window.events.loaded.wait(60)
    for _ in range(60):
        seen = window.evaluate_js("document.getElementById('empty-cloud').textContent") or ""
        if seen.startswith("Shearwater Cloud"):
            break
        time.sleep(1)
    window.destroy()


webview.start(probe)
ok = seen.startswith("Shearwater Cloud")
Path("selftest.txt").write_text(f"{'ok' if ok else 'FAILED'}: the page said {seen!r}\n")
sys.exit(0 if ok else 1)
