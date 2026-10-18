#!/bin/sh
# Build dist/bottomtime.app and dist/bottomtime-mac.zip on an Apple silicon Mac.
# Needs uv. The app is ad-hoc signed, not notarized, so the first launch needs
# System Settings > Privacy & Security > Open Anyway.
set -eu
cd "$(dirname "$0")/.."
venv=build/app-venv
uv venv -q -p 3.13 --clear "$venv"
uv pip install -q -p "$venv/bin/python" ".[app]" pyinstaller
version=$("$venv/bin/python" -c "import bottomtime; print(bottomtime.__version__)")
rm -rf dist/bottomtime.app dist/bottomtime-mac.zip
"$venv/bin/pyinstaller" --noconfirm --log-level WARN --windowed --name bottomtime \
  --specpath build --workpath build/pyinstaller --distpath dist \
  --icon "$PWD/packaging/bottomtime.icns" \
  --osx-bundle-identifier io.github.snehankekre.bottomtime \
  --collect-data bottomtime --collect-all curl_cffi \
  packaging/launch.py
plist=dist/bottomtime.app/Contents/Info.plist
plutil -replace CFBundleShortVersionString -string "$version" "$plist"
plutil -replace CFBundleVersion -string "$version" "$plist"
# Editing the plist breaks the ad-hoc signature PyInstaller made, so sign again.
codesign --force --deep --sign - dist/bottomtime.app
ditto -c -k --keepParent dist/bottomtime.app dist/bottomtime-mac.zip
echo "built bottomtime $version: dist/bottomtime.app and dist/bottomtime-mac.zip"
