#!/bin/bash
# Build RelayWatch.app (arm64 only) around the SwiftPM executable.
set -euo pipefail
cd "$(dirname "$0")/.."

swift build -c release --arch arm64
BIN="$(swift build -c release --arch arm64 --show-bin-path)"

APP=".build/RelayWatch.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/RelayWatch" "$APP/Contents/MacOS/RelayWatch"
cp Info.plist "$APP/Contents/Info.plist"

# Ad-hoc signature with the sandbox entitlements. For distribution, sign with a Developer ID.
codesign --force --sign - --options runtime \
  --entitlements RelayWatch.entitlements "$APP"

echo "built $APP"
echo "run: open \"$APP\""
