#!/usr/bin/env bash
# Запуск из исходников на Linux/macOS (основная платформа — Windows, см. run.bat).
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for v in python3.13 python3.12 python3.14 python3.11 python3.10 python3; do
  if command -v "$v" >/dev/null 2>&1 && "$v" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)'; then
    PY="$v"; break
  fi
done
[ -n "$PY" ] || { echo "Нужен Python 3.10–3.14"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
exec python -m seabattle "$@"
