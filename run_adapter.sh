#!/usr/bin/env bash
# Start the OpenAI-compatible adapter (uvicorn).
set -e
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn adapter:app --host 127.0.0.1 --port 18111