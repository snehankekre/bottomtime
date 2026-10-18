#!/bin/sh
# Build the Mac app on an Apple silicon Mac. Needs uv.
#
#   packaging/build_mac_app.sh          dist/bottomtime.app, dist/bottomtime-mac.zip
#   packaging/build_mac_app.sh intel    dist/intel/bottomtime.app, dist/bottomtime-mac-intel.zip
#
# PyInstaller bundles the Python it runs under, so each build runs on one
# architecture. The Intel build uses an x86_64 Python under Rosetta. The app is
# ad-hoc signed, not notarized, so the first launch needs System Settings >
# Privacy & Security > Open Anyway.
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" = intel ]; then
  run="arch -x86_64" python=cpython-3.13-macos-x86_64 out=dist/intel zip=dist/bottomtime-mac-intel.zip
else
  run="" python=cpython-3.13-macos-aarch64 out=dist zip=dist/bottomtime-mac.zip
fi
venv=build/app-venv-${1:-arm64}
uv venv -q -p "$python" --clear "$venv"
uv pip install -q -p "$venv/bin/python" ".[app]" pyinstaller
version=$($run "$venv/bin/python" -c "import bottomtime; print(bottomtime.__version__)")
rm -rf "$out/bottomtime.app" "$zip"
$run "$venv/bin/pyinstaller" --noconfirm --log-level WARN --windowed --name bottomtime \
  --specpath build --workpath "build/pyinstaller-${1:-arm64}" --distpath "$out" \
  --icon "$PWD/packaging/bottomtime.icns" \
  --osx-bundle-identifier io.github.snehankekre.bottomtime \
  --collect-data bottomtime --collect-all curl_cffi \
  packaging/launch.py
plist=$out/bottomtime.app/Contents/Info.plist
plutil -replace CFBundleShortVersionString -string "$version" "$plist"
plutil -replace CFBundleVersion -string "$version" "$plist"
# Editing the plist breaks the ad-hoc signature PyInstaller made, so sign again.
codesign --force --deep --sign - "$out/bottomtime.app"
ditto -c -k --keepParent "$out/bottomtime.app" "$zip"
echo "built bottomtime $version: $out/bottomtime.app and $zip"
