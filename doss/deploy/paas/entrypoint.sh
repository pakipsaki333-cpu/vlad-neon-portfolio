#!/usr/bin/env bash
#
# entrypoint.sh — Xray (VLESS + WebSocket) для бесплатного PaaS без карты
# (Render / Railway / Koyeb / любой Docker-хостинг с HTTPS-доменом).
#
# Особенность PaaS: наружу торчит только HTTPS на 443, произвольный порт не
# открыть. Поэтому TLS делает платформа, а внутри мы слушаем $PORT и отдаём
# VLESS через WebSocket — клиент видит обычный wss:// к домену платформы.
#
# Переменные:
#   PORT          порт, который даёт платформа (Render передаёт сам)
#   DOSS_UUID     UUID пользователя (постоянный! иначе ссылки умрут при рестарте)
#   DOSS_PATH     путь WebSocket (по умолчанию /doss-ws)
#   DOSS_HOST     домен, на который будет ходить клиент (для печати ссылки)
#   DOSS_PRINT_CONFIG=1  только напечатать конфиг и выйти (для тестов/отладки)
#
set -euo pipefail

PORT="${PORT:-8080}"
WS_PATH="${DOSS_PATH:-/doss-ws}"
HOST="${DOSS_HOST:-${RENDER_EXTERNAL_HOSTNAME:-localhost}}"
LOGLEVEL="${DOSS_LOGLEVEL:-warning}"

# UUID должен переживать рестарты: берём из env, иначе выводим стабильно
# из идентификатора сервиса, иначе — из фиксированного секрета.
if [[ -z "${DOSS_UUID:-}" ]]; then
  seed="${DOSS_SECRET:-${RENDER_SERVICE_ID:-doss-static-seed}}"
  DOSS_UUID="$(python3 - "$seed" <<'PY'
import sys, uuid
seed = sys.argv[1] if len(sys.argv) > 1 else "doss"
print(uuid.uuid5(uuid.NAMESPACE_URL, "doss:" + seed))
PY
)"
  echo "[doss] DOSS_UUID не задан — вывожу стабильный UUID из секрета сервиса." >&2
  echo "[doss] Чтобы UUID не зависел ни от чего, задай переменную DOSS_UUID." >&2
fi

CONFIG=/tmp/xray-config.json
cat > "$CONFIG" <<EOF
{
  "log": { "loglevel": "$LOGLEVEL", "access": "none" },
  "inbounds": [
    {
      "tag": "vless-ws",
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": {
        "clients": [ { "id": "${DOSS_UUID}", "email": "doss" } ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "ws",
        "security": "none",
        "wsSettings": { "path": "${WS_PATH}" }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": [ { "type": "field", "protocol": ["bittorrent"], "outboundTag": "block" } ]
  }
}
EOF

if [[ "${DOSS_PRINT_CONFIG:-0}" == "1" ]]; then
  cat "$CONFIG"
  exit 0
fi

# Ссылка: клиент → HTTPS/WSS на домен платформы, путь наш
ENCODED_PATH="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1], safe=""))' "$WS_PATH")"
LINK="vless://${DOSS_UUID}@${HOST}:443?encryption=none&security=tls&type=ws&host=${HOST}&path=${ENCODED_PATH}&sni=${HOST}&fp=chrome#Doss%20WS"

cat <<EOF

──────────────────────────────────────────────────────────────
 Doss · WebSocket-режим (бесплатный хостинг)
──────────────────────────────────────────────────────────────
 Домен   : ${HOST}
 Порт    : 443 (TLS делает платформа)
 Путь WS : ${WS_PATH}
 UUID    : ${DOSS_UUID}

 ССЫЛКА ДЛЯ КЛИЕНТА (скопируй целиком):

${LINK}

 Как подключить:
   1. Установи Hiddify или v2rayNG
   2. «+» → «Импорт из буфера обмена» → вставить ссылку
   3. Подключиться (первый коннект после простоя ~30–60 сек: сервис просыпается)

 ВАЖНО: этот UUID зашит в приложение. Если хочешь выдать доступ другим —
   задай DOSS_UUID в переменных сервиса и пересобери.
──────────────────────────────────────────────────────────────

EOF

if [[ "${DOSS_DRY_RUN:-0}" == "1" ]]; then
  echo "[doss] DOSS_DRY_RUN=1 — конфиг и ссылка готовы, xray не запускаю"
  exit 0
fi

exec /usr/local/bin/xray run -config "$CONFIG"
