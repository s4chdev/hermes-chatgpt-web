#!/usr/bin/env bash
# Start the SPA gateway (owns the Chrome session). Ensures Xvfb :99 first.
set -e
cd "$(dirname "$0")"

if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb99.log 2>&1 &
  sleep 2
fi

exec .venv/bin/python gateway.py 18110