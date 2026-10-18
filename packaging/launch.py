"""Entry point for the packaged Mac app. --browser shows the UI in a browser
tab, which is also how the built app gets tested."""

import sys

from bottomtime.app import run

sys.exit(run(browser="--browser" in sys.argv))
