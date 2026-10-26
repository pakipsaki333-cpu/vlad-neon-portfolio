#!/usr/bin/env bash
#
# Тесты CLI `doss` без сервера: временный /etc/doss, заглушка вместо Xray.
# Запуск:  bash doss/server/tests/test_cli.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/../doss"
ROOT="$(mktemp -d)"
export DOSS_ETC="$ROOT/etc"
export SUB_DIR="$ROOT/sub"
export BASE_CONFIG="$DOSS_ETC/base-config.json"
export XRAY_CONFIG="$ROOT/xray/config.json"
export XRAY_BIN="$ROOT/no-such-xray"
export DOSS_NO_SERVICE=1

mkdir -p "$DOSS_ETC" "$SUB_DIR"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

# --- состояние, как его создаёт install.sh
jq -n '{version:1, created:"2026-01-01T00:00:00Z",
  server:{host:"203.0.113.10", port:443, dest_domain:"www.microsoft.com",
    private_key:"PRIVKEY_PRIVKEY_PRIVKEY_PRIVKEY_PRIVKEY=",
    public_key:"PUBKEY_PUBKEY_PUBKEY_PUBKEY_PUBKEY=",
    short_ids:["","aaaaaaaa","bbbbbbbb"],
    xhttp:{enabled:true, port:8443, path:"/doss-xh"},
    awg:{enabled:false}},
  users:{}}' > "$DOSS_ETC/state.json"

echo "== build-base"
"$CLI" build-base >/dev/null 2>&1
check "шаблон создан" "[[ -s '$BASE_CONFIG' ]]"
check "в шаблоне 2 инбаунда (tcp + xhttp)" "[[ \$(jq '.inbounds|length' '$BASE_CONFIG') == 2 ]]"
check "reality dest = domain:443" "[[ \$(jq -r '.inbounds[0].streamSettings.realitySettings.dest' '$BASE_CONFIG') == 'www.microsoft.com:443' ]]"
check "xhttp network" "[[ \$(jq -r '.inbounds[1].streamSettings.network' '$BASE_CONFIG') == 'xhttp' ]]"

echo "== user add"
OUT="$("$CLI" user add anna --expires 2026-12-31 --traffic 200 2>/dev/null)"
check "ссылка выдана" "[[ '$OUT' == vless://* ]]"
check "uuid в state" "[[ \$(jq -r '.users.anna.uuid' '$DOSS_ETC/state.json') =~ ^[0-9a-f-]{36}$ ]]"
check "flow vision в ссылке" "[[ '$OUT' == *'flow=xtls-rprx-vision'* ]]"
check "pbk = публичный ключ" "[[ '$OUT' == *'pbk=PUBKEY_PUBKEY_PUBKEY_PUBKEY_PUBKEY='* ]]"
check "sid из state" "[[ '$OUT' == *'sid='\$(jq -r '.users.anna.short_id' '$DOSS_ETC/state.json')* ]]"
check "клиент попал в конфиг Xray" "[[ \$(jq '[.inbounds[0].settings.clients[]|select(.email==\"anna\")]|length' '$XRAY_CONFIG') == 1 ]]"
check "у xhttp-клиента нет flow" "[[ \$(jq -r '.inbounds[1].settings.clients[0].flow // \"none\"' '$XRAY_CONFIG') == 'none' ]]"
check "персональный short_id добавлен в сервер" "[[ \$(jq '.inbounds[0].streamSettings.realitySettings.shortIds|length' '$XRAY_CONFIG') -ge 4 ]]"

echo "== второй пользователь"
"$CLI" user add boris --unlimited >/dev/null 2>&1
check "двое активных в конфиге" "[[ \$(jq '.inbounds[0].settings.clients|length' '$XRAY_CONFIG') == 2 ]]"
check "дубликат имени запрещён" "! '$CLI' user add anna >/dev/null 2>&1"

echo "== list / link / qr / sub"
check "список печатается" "'$CLI' user list | grep -q anna"
check "link = одна строка vless" "[[ \$('$CLI' link boris | wc -l) == 1 ]]"
SUBFILE="$("$CLI" sub anna 2>&1 | sed -n 's/^файл подписки: //p')"
check "sub-файл создан" "[[ -f '$SUBFILE' ]]"
check "url-base склеивается" "[[ \$('$CLI' sub anna --url-base http://203.0.113.10 2>/dev/null | tail -1) == http://203.0.113.10/sub/*.txt ]]"

echo "== disable / enable"
"$CLI" user set boris --disable >/dev/null 2>&1
check "выключенный не попадает в конфиг" "[[ \$(jq '.inbounds[0].settings.clients|length' '$XRAY_CONFIG') == 1 ]]"
"$CLI" user set boris --enable >/dev/null 2>&1
check "включённый вернулся" "[[ \$(jq '.inbounds[0].settings.clients|length' '$XRAY_CONFIG') == 2 ]]"

echo "== tick (истечение)"
"$CLI" user add old --expires 2020-01-01 >/dev/null 2>&1
"$CLI" tick >/dev/null 2>&1
check "истёкший отключён" "[[ \$(jq -r '.users.old.enabled' '$DOSS_ETC/state.json') == false ]]"
check "истёкший убран из конфига" "[[ \$(jq '.inbounds[0].settings.clients|length' '$XRAY_CONFIG') == 2 ]]"
STATUS="$("$CLI" status)"
check "статус считает активных" "grep -q '3 всего, 2 активных' <<< \"\$STATUS\""

echo "== revoke"
"$CLI" user rm anna >/dev/null 2>&1
check "anna удалена из state" "[[ \$(jq -r '.users.anna // \"none\"' '$DOSS_ETC/state.json') == none ]]"
check "anna удалена из конфига" "! jq -e '.inbounds[0].settings.clients[]|select(.email==\"anna\")' '$XRAY_CONFIG'"
check "sub-файл anna удалён" "[[ \$(ls '$SUB_DIR' | wc -l) -eq 2 ]]"

echo
echo "итог: $PASS ок, $FAIL провал(ов); временные файлы: $ROOT"
[[ "$FAIL" -eq 0 ]] || exit 1
