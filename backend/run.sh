#!/bin/zsh
# Lifeline backend — run from your FDA terminal (needs Full Disk Access for
# iMessage and GOOGLE_CLIENT_ID/SECRET + GEMINI_API_KEY in the environment).
#
# Uses the repo venv's python so it works regardless of what `python3` means
# in the current shell. --reload is not optional: it picks up every code
# change automatically, so the server can never silently fall behind again.
cd "$(dirname "$0")"

# Secrets that shouldn't live in the repo or in a shell rc. Optional: without
# it the server still runs, and APNs simply stays in dry-run.
[ -f "$HOME/.lifeline/secrets/env" ] && source "$HOME/.lifeline/secrets/env"

exec ./.venv/bin/python -m uvicorn lifeline.api.app:app --host 0.0.0.0 --port 8000 --reload
