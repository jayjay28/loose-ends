#!/bin/bash
# The Loose Ends engine installer (§v3 workstream 1).
#
# One run takes a Mac from a source checkout to a running engine with the
# setup wizard open in the browser. Idempotent: running it again updates
# dependencies and reloads the job instead of failing.
#
#   ./install.sh              install and open the wizard
#   ./install.sh --dry-run    say every step without doing any of them
#
# Overrides (mostly for testing an install beside a live engine):
#   PORT=8100 LABEL=com.looseends.api.test DB=/tmp/test.db ./install.sh
#
# The public `curl | sh` bootstrap (first release) clones the repo and then
# runs exactly this file — keep it self-contained.
set -euo pipefail

PORT="${PORT:-8000}"
LABEL="${LABEL:-com.looseends.api}"
DB="${DB:-}"
DRY="${1:-}"

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$HOME/Library/Logs/loose-ends"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_DIR="$HOME/.lifeline"

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*"; }
run()  { if [ "$DRY" = "--dry-run" ]; then echo "  would: $*"; else "$@"; fi }

say "Loose Ends engine installer"
say "engine source: $BACKEND"

# ---------------------------------------------------------------- guards
if [ "$LABEL" = "com.looseends.api" ] && launchctl list 2>/dev/null | grep -q "com.lifeline.api"; then
  warn "a Lifeline-era engine (com.lifeline.api) is already running on this Mac."
  warn "this installer won't stack a second engine on it — nothing was changed."
  exit 1
fi
if [ "$DRY" != "--dry-run" ] && lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
   && ! launchctl list 2>/dev/null | grep -q "$LABEL"; then
  warn "something else is already listening on port $PORT — nothing was changed."
  warn ""
  warn "  see what has it:   lsof -i :$PORT"
  warn "  or use another:    PORT=8100 $0"
  exit 1
fi

# ------------------------------------------------------------ the runtime
# uv when it's there (fast, brings its own Python); the system python3
# otherwise (ships with the Xcode command-line tools, 3.9+ is enough).
say "installing the engine's dependencies — 1 to 3 minutes, and the quietest part of this"
if command -v uv >/dev/null 2>&1; then
  say "creating the runtime with uv…"
  run uv venv --quiet --python ">=3.11" "$BACKEND/.venv" || run uv venv --quiet "$BACKEND/.venv"
  run uv pip install --quiet -r "$BACKEND/requirements.txt" --python "$BACKEND/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  say "creating the runtime with the system python3 ($(python3 --version 2>&1))…"
  if [ ! -x "$BACKEND/.venv/bin/python" ]; then
    run python3 -m venv "$BACKEND/.venv"
  fi
  run "$BACKEND/.venv/bin/python" -m pip install --quiet --upgrade pip
  run "$BACKEND/.venv/bin/python" -m pip install --quiet -r "$BACKEND/requirements.txt"
else
  warn "no python3 found — install the Xcode command-line tools first:"
  warn "    xcode-select --install"
  exit 1
fi

# --------------------------------------------------------------- the home
say "credentials home: $ENV_DIR/env (written by the setup wizard, never a shell rc)"
run mkdir -p "$ENV_DIR" "$LOGS"
run chmod 700 "$ENV_DIR"

# ----------------------------------------------------------- the launchd job
# zsh wraps the process on purpose: macOS attributes the Full Disk Access
# grant to /bin/zsh (a hard-won TCC lesson), so the wizard can say
# "add zsh" and have it stay true. The engine reads ~/.lifeline/env itself —
# no shell rc is sourced, nothing hides in anyone's dotfiles.
ENV_LINE=""
if [ -n "$DB" ]; then
  ENV_LINE="<key>EnvironmentVariables</key><dict><key>LIFELINE_DB</key><string>$DB</string></dict>"
fi

say "installing the launchd job ($LABEL, port $PORT)…"
if [ "$DRY" = "--dry-run" ]; then
  echo "  would: write $PLIST and bootstrap it"
else
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProcessType</key><string>Background</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  $ENV_LINE
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>cd $BACKEND &amp;&amp; export LIFELINE_PORT=$PORT LIFELINE_MANAGED=1 &amp;&amp; exec .venv/bin/python -m uvicorn lifeline.api.app:app --host 0.0.0.0 --port $PORT</string>
  </array>
  <key>StandardOutPath</key><string>$LOGS/$LABEL.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$LABEL.err.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi

# ------------------------------------------------------------- the command
# `lifeline doctor` and `lifeline pair` are named in the support page, in the
# doctor's own advice, and on the pairing screen. None of them worked: there
# was no such command anywhere on PATH.
SHIM="/usr/local/bin/lifeline"
if [ "$DRY" = "--dry-run" ]; then
  echo "  would: install the lifeline command at $SHIM"
elif [ -w "$(dirname "$SHIM")" ] 2>/dev/null || [ -w "$SHIM" ] 2>/dev/null; then
  cat > "$SHIM" <<SHIMEOF
#!/bin/sh
exec "$BACKEND/.venv/bin/python" -m lifeline.cli "\$@"
SHIMEOF
  chmod +x "$SHIM"
  say "the 'lifeline' command is available (try: lifeline doctor)"
else
  say "note: couldn't write $SHIM — run the CLI as:"
  say "      $BACKEND/.venv/bin/python -m lifeline.cli doctor"
fi

# ------------------------------------------------------------- the handoff
# --------------------------------------------------------- the menu bar app
# The one thing that answers "is it running, and how do I stop it" — built,
# signed, notarized, and until now installed by nothing.
MENUBAR="$BACKEND/../mac/build/Loose Ends.app"
if [ -d "$MENUBAR" ] && [ "$DRY" != "--dry-run" ]; then
  say "installing the menu bar app"
  rm -rf "/Applications/Loose Ends.app" 2>/dev/null || true
  cp -R "$MENUBAR" /Applications/ 2>/dev/null && open -g "/Applications/Loose Ends.app" 2>/dev/null || true
fi

say "waiting for the engine to answer…"
if [ "$DRY" = "--dry-run" ]; then
  echo "  would: poll http://localhost:$PORT and open the setup wizard"
else
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null "http://localhost:$PORT/setup"; then
      echo ""
      say "✓ the engine is RUNNING — http://localhost:$PORT"
      say "  it starts with your Mac from now on (launchd job: $LABEL)"
      say "  opening the setup wizard: http://localhost:$PORT/setup"
      open "http://localhost:$PORT/setup"
      say "done. The wizard takes it from here."
      exit 0
    fi
    sleep 1
  done
  warn "the engine didn't answer within 30s — check $LOGS/$LABEL.err.log"
  exit 1
fi
