#!/usr/bin/env bash
#
# Тесты PaaS-режима: конфиг Xray, стабильность UUID, формат ссылки.
# Не требует Docker, Render и установленного xray.
#
#   bash doss/deploy/paas/tests/test_paas.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EP="$HERE/../entrypoint.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n' "$*"; }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

UUID=11111111-2222-4333-8444-555555555555

echo "== конфиг"
CFG="$(DOSS_PRINT_CONFIG=1 PORT=10000 DOSS_UUID=$UUID "$EP")"
check "валидный JSON" "printf '%s' \"\$CFG\" | jq -e . "
check "слушает порт платформы (10000)" "[[ \$(printf '%s' \"\$CFG\" | jq -r '.inbounds[0].port') == 10000 ]]"
check "транспорт ws" "[[ \$(printf '%s' \"\$CFG\" | jq -r '.inbounds[0].streamSettings.network') == ws ]]"
check "путь /doss-ws" "[[ \$(printf '%s' \"\$CFG\" | jq -r '.inbounds[0].streamSettings.wsSettings.path') == /doss-ws ]]"
check "uuid из env" "[[ \$(printf '%s' \"\$CFG\" | jq -r '.inbounds[0].settings.clients[0].id') == $UUID ]]"
check "торренты заблокированы" "printf '%s' \"\$CFG\" | jq -e '.routing.rules[] | select(.protocol[0]==\"bittorrent\")'"
check "кастомный путь подставляется" \
  "[[ \$(DOSS_PRINT_CONFIG=1 DOSS_PATH=/x9 PORT=80 DOSS_UUID=$UUID \"\$EP\" | jq -r '.inbounds[0].streamSettings.wsSettings.path') == /x9 ]]"

echo "== UUID стабилен между рестартами"
A="$(DOSS_PRINT_CONFIG=1 RENDER_SERVICE_ID=srv_abc "$EP" 2>/dev/null | jq -r '.inbounds[0].settings.clients[0].id')"
B="$(DOSS_PRINT_CONFIG=1 RENDER_SERVICE_ID=srv_abc "$EP" 2>/dev/null | jq -r '.inbounds[0].settings.clients[0].id')"
C="$(DOSS_PRINT_CONFIG=1 RENDER_SERVICE_ID=srv_xyz "$EP" 2>/dev/null | jq -r '.inbounds[0].settings.clients[0].id')"
check "два запуска — один UUID" "[[ \"\$A\" == \"\$B\" ]]"
check "другой сервис — другой UUID" "[[ \"\$A\" != \"\$C\" ]]"
check "это uuid v5" "[[ \"\$A\" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}- ]]"

echo "== ссылка для клиента"
OUT="$(DOSS_DRY_RUN=1 PORT=8080 RENDER_EXTERNAL_HOSTNAME=demo.onrender.com DOSS_UUID=$UUID "$EP" 2>/dev/null)"
LINK="$(printf '%s\n' "$OUT" | grep '^vless://' | head -1)"
check "ссылка напечатана" "[[ -n \"\$LINK\" ]]"
check "ws + tls" "[[ \"\$LINK\" == *'type=ws'* && \"\$LINK\" == *'security=tls'* ]]"
check "порт 443 (TLS у платформы)" "[[ \"\$LINK\" == *'@demo.onrender.com:443'* ]]"
check "sni = домен платформы" "[[ \"\$LINK\" == *'sni=demo.onrender.com'* ]]"
check "путь url-encoded" "[[ \"\$LINK\" == *'path=%2Fdoss-ws'* ]]"
check "fingerprint chrome" "[[ \"\$LINK\" == *'fp=chrome'* ]]"
check "uuid в ссылке" "[[ \"\$LINK\" == *\"$UUID\"* ]]"

echo
echo "итог: $PASS ок, $FAIL провал(ов)"
[[ "$FAIL" -eq 0 ]] || exit 1
