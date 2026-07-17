#!/bin/bash
# Medical Image Viewer — double-click launcher for macOS.
# On first run it sets up everything automatically; later runs start instantly.

cd "$(dirname "$0")" || exit 1

fail() { osascript -e "display dialog \"$1\" buttons {\"OK\"} with icon stop" >/dev/null 2>&1; exit 1; }

# 1. Find a suitable Python (3.10+)
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  fail "Python 3.10 or newer is required but was not found.\n\nPlease install it from https://www.python.org/downloads/ and then double-click this file again."
fi

# 2. First-time setup: create the environment and install dependencies
if [ ! -d ".venv" ]; then
  echo "First-time setup: creating environment (this can take a few minutes)…"
  "$PY" -m venv .venv || fail "Could not create the Python environment."
fi
if [ ! -f ".venv/.deps_ok" ]; then
  echo "Installing dependencies… please wait."
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
  if ./.venv/bin/python -m pip install -r requirements.txt; then
    touch .venv/.deps_ok
  else
    fail "Could not install the required packages. Check your internet connection and try again."
  fi
fi

# 3. Launch
echo ""
echo "==============================================="
echo "  Medical Image Viewer is starting — your browser will"
echo "  open automatically in a moment."
echo ""
echo "  Keep this window open while you work."
echo "  To stop: click \"Quit\" in the app, or just"
echo "  close this window."
echo "==============================================="
echo ""
exec ./.venv/bin/python app.py
