#!/bin/bash
# Update an already-installed /Applications/Odysseus.app in-place.
#
#   ./update-macos-app.sh
#
# Rebuilds Odysseus.app from the current repo and hot-swaps the contents
# inside /Applications/Odysseus.app — no DMG, no drag-and-drop, no uninstall.
#
# If Odysseus.app is not yet in /Applications, it falls back to opening the
# freshly built dist/Odysseus.app directly from the repo.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Odysseus"
INSTALLED_APP="/Applications/$APP_NAME.app"
DIST="$REPO_DIR/dist"
BUILT_APP="$DIST/$APP_NAME.app"

echo "▶ Rebuilding $APP_NAME.app…"
bash "$REPO_DIR/build-macos-app.sh"

if [ -d "$INSTALLED_APP" ]; then
  echo ""
  echo "▶ Hot-swapping $INSTALLED_APP…"

  # Quit the running app gracefully before replacing binaries.
  if pgrep -x "$APP_NAME" >/dev/null 2>&1 || pgrep -f "MacOS/$APP_NAME" >/dev/null 2>&1; then
    echo "  Quitting running Odysseus…"
    osascript -e 'quit app "Odysseus"' >/dev/null 2>&1 || true
    sleep 1
  fi

  # Replace only the Contents directory — preserves any macOS extended
  # attributes / quarantine flags on the .app bundle itself.
  rm -rf "$INSTALLED_APP/Contents"
  cp -R "$BUILT_APP/Contents" "$INSTALLED_APP/Contents"

  # Tell Finder / Launch Services about the updated bundle.
  touch "$INSTALLED_APP"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$INSTALLED_APP" >/dev/null 2>&1 || true

  echo "  ✓ $INSTALLED_APP updated"
  echo ""
  echo "Launch:  open '$INSTALLED_APP'"
else
  echo ""
  echo "  ℹ  Odysseus is not installed in /Applications."
  echo "     Opening from dist instead: $BUILT_APP"
  echo ""
  echo "  To install permanently:"
  echo "    open '$DIST/$APP_NAME.dmg'  (drag Odysseus → Applications)"
  echo "  Then run this script for future updates."
  open "$BUILT_APP"
fi
