#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install --quiet -r requirements.txt
echo "Price Hunter: http://127.0.0.1:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
