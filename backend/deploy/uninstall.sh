#!/bin/bash
# Take Loose Ends off this Mac (§v3.1).
#
#   ./uninstall.sh              stop and remove the engine; keep your data
#   ./uninstall.sh --everything also delete the database and credentials
#   ./uninstall.sh --dry-run    say what would go, touch nothing
#
# Why this exists: a first run that wedged left a launchd job with
# KeepAlive=true crash-looping every ten seconds, and nothing anywhere to
# undo it. "I tried your thing and now something runs on my Mac forever"
# is a worse outcome than a failed install, and it was the only outcome
# available.
#
# Stopping and erasing are deliberately different commands. The database is
# a reading of someone's life; removing the software should never take it
# without being asked in so many words.
set -uo pipefail

LABEL="${LABEL:-com.looseends.api}"
BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/loose-ends"
ENV_DIR="$HOME/.lifeline"
APP="/Applications/Loose Ends.app"
SHIM="/usr/local/bin/lifeline"
DB="${LIFELINE_DB:-$BACKEND/lifeline.db}"

MODE="${1:-}"
DRY=0
ALL=0
case "$MODE" in
  --dry-run)    DRY=1 ;;
  --everything) ALL=1 ;;
  "")           ;;
  *) echo "usage: $0 [--everything|--dry-run]" >&2; exit 2 ;;
esac

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*"; }
act()  { if [ "$DRY" -eq 1 ]; then echo "  would: $*"; else "$@" >/dev/null 2>&1; fi }

say "Loose Ends uninstaller"
[ "$DRY" -eq 1 ] && say "dry run — nothing will be changed"

# ------------------------------------------------------------- the engine
# Read the port out of the launch agent before removing it: it is the only
# thing that distinguishes *this* engine from another one running beside it,
# and the by-hand cleanup below needs it. Testing this script found the
# alternative the hard way — a bare `pkill -f uvicorn lifeline.api.app`
# uninstalling a test engine on 8123 killed the live one on 8000 too.
ENGINE_PORT=""
if [ -f "$PLIST" ]; then
  ENGINE_PORT="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$PLIST" | head -1)"
fi

# bootout first, then remove the plist: unloading a job whose file is already
# gone leaves launchd holding a copy until logout.
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  say "stopping the engine ($LABEL)"
  act launchctl bootout "gui/$(id -u)/$LABEL"
else
  say "the engine isn't running"
fi
if [ -f "$PLIST" ]; then
  say "removing the launch agent, so it won't come back at login"
  act rm -f "$PLIST"
fi

# An engine started by hand, outside launchd — but only *this* one. Another
# engine on another port belongs to somebody else's install and is not ours
# to stop.
if [ -n "$ENGINE_PORT" ] && pgrep -f "uvicorn lifeline.api.app.*--port $ENGINE_PORT" >/dev/null 2>&1; then
  say "stopping an engine on port $ENGINE_PORT that was started by hand"
  act pkill -f "uvicorn lifeline.api.app.*--port $ENGINE_PORT"
elif [ -z "$ENGINE_PORT" ]; then
  # No launch agent to read a port from. Say what we can see rather than
  # killing anything by guess.
  if pgrep -f "uvicorn lifeline.api.app" >/dev/null 2>&1; then
    say "an engine is running but no launch agent names it — leaving it alone."
    say "  stop it yourself with: pkill -f 'uvicorn lifeline.api.app'"
  fi
fi

# --------------------------------------------------------------- the rest
if [ -d "$APP" ]; then
  say "removing the menu bar app"
  act pkill -f "Loose Ends.app/Contents/MacOS"
  act rm -rf "$APP"
fi

if [ -e "$SHIM" ]; then
  if [ -w "$SHIM" ] || [ -w "$(dirname "$SHIM")" ]; then
    say "removing the lifeline command"
    act rm -f "$SHIM"
  else
    warn "couldn't remove $SHIM (needs admin) — delete it by hand:"
    warn "    sudo rm $SHIM"
  fi
fi

if [ -d "$LOGS" ]; then
  say "removing the logs"
  act rm -rf "$LOGS"
fi

# ----------------------------------------------------------------- data
if [ "$ALL" -eq 1 ]; then
  warn "--everything: deleting your data as well"
  for path in "$DB" "$DB-wal" "$DB-shm" "$ENV_DIR"; do
    if [ -e "$path" ]; then
      say "  deleting $path"
      act rm -rf "$path"
    fi
  done
else
  say ""
  say "your data was kept:"
  [ -f "$DB" ]      && say "  $DB  ($(du -h "$DB" 2>/dev/null | cut -f1)) — everything it ever read"
  [ -d "$ENV_DIR" ] && say "  $ENV_DIR — your API key"
  say "  delete both with: $0 --everything"
fi

say ""
if [ "$DRY" -eq 1 ]; then
  say "dry run finished — nothing was changed."
else
  say "done. Loose Ends is no longer running or starting at login."
  say "the source is still at $BACKEND — delete that folder to finish."
fi
