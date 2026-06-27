#!/bin/bash
# Build a downloadable macOS launcher app + .dmg for Odysseus.
#
#   ./build-macos-app.sh
#
# Produces:
#   dist/Odysseus.app   — double-click: starts the local server (using this
#                         repo's venv) and opens the UI in an app-style window.
#   dist/Odysseus.dmg   — drag-to-Applications disk image (the downloadable).
#
# This is a *launcher* wrapper: it drives the venv we set up in this repo, it
# does not bundle Python. The install path is baked into the app at build time,
# so rebuild if you move the repo. Override the port with ODYSSEUS_PORT.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Odysseus"
INSTALL_DIR="$REPO_DIR"
PORT="${ODYSSEUS_PORT:-7860}"
DIST="$REPO_DIR/dist"
APP="$DIST/$APP_NAME.app"

echo "Building $APP_NAME.app"
echo "  install dir: $INSTALL_DIR"
echo "  port:        $PORT"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── Icon (best effort) — center-crop docs/odysseus.jpg to a square .icns ──
if [ -f "$REPO_DIR/docs/odysseus.jpg" ] && command -v sips >/dev/null 2>&1; then
  TMPIMG="$(mktemp -d)"
  # Center-crop to a square, scale to 512 (sips' icns encoder caps at 512), and
  # let sips emit the .icns directly — more robust across macOS versions than
  # building an .iconset by hand.
  sips -c 720 720 "$REPO_DIR/docs/odysseus.jpg" --out "$TMPIMG/sq.png" >/dev/null 2>&1 || cp "$REPO_DIR/docs/odysseus.jpg" "$TMPIMG/sq.png"
  sips -z 512 512 "$TMPIMG/sq.png" --out "$TMPIMG/icon.png" >/dev/null 2>&1
  if sips -s format icns "$TMPIMG/icon.png" --out "$APP/Contents/Resources/odysseus.icns" >/dev/null 2>&1; then
    echo "  icon:        odysseus.icns"
  else
    echo "  icon:        (skipped — conversion failed)"
  fi
  rm -rf "$TMPIMG"
else
  echo "  icon:        (skipped — no docs/odysseus.jpg)"
fi

# ── Native WKWebView helper (OdysseusUI) ──
# Compiles a tiny Swift binary that opens a proper app-window using WebKit.
# This runs as part of the Odysseus.app process so macOS shows Odysseus's icon
# instead of Chrome's (which happens when we use Chrome's --app= flag).
SWIFT_SRC="$(mktemp /tmp/OdysseusUI_XXXXXX.swift)"
cat > "$SWIFT_SRC" <<'SWIFT'
import Cocoa
import WebKit

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let url = CommandLine.arguments.count > 1
            ? URL(string: CommandLine.arguments[1])!
            : URL(string: "http://127.0.0.1:7860")!

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.load(URLRequest(url: url))

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Odysseus"
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let delegate = AppDelegate()
NSApplication.shared.delegate = delegate
NSApplication.shared.run()
SWIFT

HELPER="$APP/Contents/MacOS/OdysseusUI"
echo "Building OdysseusUI helper…"
if swiftc -O -o "$HELPER" "$SWIFT_SRC" \
    -framework Cocoa -framework WebKit 2>/dev/null; then
    echo "  ✓ OdysseusUI compiled"
else
    echo "  ⚠ swiftc not found or failed — falling back to system browser for UI window"
fi
rm -f "$SWIFT_SRC"

# ── Info.plist ──
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>com.odysseus.launcher</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>        <string>odysseus</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSUIElement</key>             <false/>
    <key>LSArchitecturePriority</key>
    <array>
        <string>arm64</string>
        <string>x86_64</string>
    </array>
</dict>
</plist>
PLIST

# ── Launcher executable (placeholders filled below) ──
cat > "$APP/Contents/MacOS/$APP_NAME.tmpl" <<'LAUNCHER'
#!/bin/bash
# Odysseus.app — start the local server and open the UI in an app window.

# On Apple Silicon, .app bundles can be launched as x86_64 (Rosetta) even when
# the venv was built for arm64 — causing dlopen errors. Re-exec as arm64 if needed.
if [ "$(uname -m)" != "arm64" ] && /usr/bin/arch -arm64 /bin/true 2>/dev/null; then
  exec /usr/bin/arch -arm64 "$0" "$@"
fi

INSTALL_DIR="__INSTALL_DIR__"
PORT="__PORT__"
URL="http://127.0.0.1:${PORT}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

UVICORN="$INSTALL_DIR/venv/bin/uvicorn"
LOG="$INSTALL_DIR/logs/odysseus-app.log"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Odysseus\"" >/dev/null 2>&1; }
die_gui() {
  /usr/bin/osascript -e "display dialog \"$1\" with title \"Odysseus\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1
  exit 1
}

[ -x "$UVICORN" ] || die_gui "Odysseus isn't set up yet. Open Terminal and run:

cd $INSTALL_DIR
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python setup.py"

# Open the UI using the bundled native WKWebView helper (shows Odysseus icon,
# not Chrome's). Falls back to the system browser if the helper is missing.
open_ui() {
  local helper
  helper="$(dirname "$0")/OdysseusUI"
  if [ -x "$helper" ]; then
    "$helper" "$URL" &
  else
    /usr/bin/open "$URL"
  fi
}

mkdir -p "$INSTALL_DIR/logs"

# Already running? Just open the UI.
if /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; then
  open_ui
  exit 0
fi

notify "Starting…"
cd "$INSTALL_DIR" || die_gui "Install folder not found: $INSTALL_DIR"

# ── SearXNG (background, optional) ──
# Expects searxng cloned as a sibling of the Odysseus repo:
#   git clone https://github.com/searxng/searxng.git /path/to/perProjects/searxng
#   cd /path/to/perProjects/searxng && make install
# Default port: 8888 (as set in searx/settings.yml by make install).
SEARXNG_PID=""
SEARXNG_DIR="$(dirname "$INSTALL_DIR")/searxng"
SEARXNG_VENV_PYTHON="$SEARXNG_DIR/venv/bin/python"
SEARXNG_SETTINGS="$SEARXNG_DIR/searx/settings.yml"
SEARXNG_LOG="$INSTALL_DIR/logs/searxng-app.log"
if [ -f "$SEARXNG_SETTINGS" ] && [ -x "$SEARXNG_VENV_PYTHON" ]; then
  export SEARXNG_INSTANCE="http://127.0.0.1:8888"
  # Must cd into searxng dir so Python finds the searx package from source.
  # Use a pidfile to capture PID across the subshell boundary.
  _SEARXNG_PIDFILE="$(mktemp)"
  (cd "$SEARXNG_DIR" && SEARXNG_SETTINGS_PATH="$SEARXNG_SETTINGS" \
    nohup "$SEARXNG_VENV_PYTHON" -m searx.webapp >"$SEARXNG_LOG" 2>&1 & echo $! >"$_SEARXNG_PIDFILE")
  SEARXNG_PID="$(cat "$_SEARXNG_PIDFILE" 2>/dev/null)"
  rm -f "$_SEARXNG_PIDFILE"
fi

if [ "$(uname -m)" = "arm64" ]; then
  arch -arm64 "$UVICORN" app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
else
  "$UVICORN" app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
fi
SERVER_PID=$!

# Quitting the app stops the server and SearXNG.
trap 'kill $SERVER_PID 2>/dev/null; [ -n "$SEARXNG_PID" ] && kill "$SEARXNG_PID" 2>/dev/null; exit 0' TERM INT

# Wait for readiness (first run downloads an embedding model — allow ~2 min).
READY=0
for i in $(seq 1 120); do
  /usr/bin/curl -s -o /dev/null --max-time 2 "$URL" && { READY=1; break; }
  kill -0 "$SERVER_PID" 2>/dev/null || die_gui "Odysseus failed to start. Log:
$LOG"
  sleep 1
done

if [ "$READY" = "1" ]; then
  open_ui
else
  notify "Odysseus is taking a while — open $URL once it finishes starting."
fi
wait "$SERVER_PID"
LAUNCHER

sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__PORT__|$PORT|g" \
    "$APP/Contents/MacOS/$APP_NAME.tmpl" > "$APP/Contents/MacOS/$APP_NAME"
rm -f "$APP/Contents/MacOS/$APP_NAME.tmpl"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# Refresh Finder's icon cache for the new bundle.
touch "$APP"

# ── .dmg (drag-to-Applications) ──
echo "Packaging dist/$APP_NAME.dmg"
STAGE="$(mktemp -d)/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DIST/$APP_NAME.dmg"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg" >/dev/null
rm -rf "$STAGE"

echo ""
echo "Done:"
echo "  $APP"
echo "  $DIST/$APP_NAME.dmg"
echo ""
echo "Run it:        open '$APP'"
echo "Install:       open '$DIST/$APP_NAME.dmg'  (drag Odysseus to Applications)"
