#!/usr/bin/env bash
# NeonMiner — запуск веб-клиента. Работает на чистом Python 3.9+ без pip install.
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "NeonMiner → http://${HOST}:${PORT}"
exec python3 -m neon_miner serve --host "$HOST" --port "$PORT"
