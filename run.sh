#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

HOST="${LUMEN_HOST:-127.0.0.1}"
PORT="${LUMEN_PORT:-5024}"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -e .

echo "📡 Lumenfeed starting on http://localhost:${PORT}"
uvicorn lumenfeed.api:app --host "${HOST}" --port "${PORT}"
