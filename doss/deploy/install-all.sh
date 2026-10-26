#!/usr/bin/env bash
#
# install-all.sh — Doss «под ключ» на свежем Ubuntu 22.04/24.04 или Debian 12.
#
# Одна команда ставит ВСЁ: VPN-узел (VLESS+REALITY), учёт трафика, панель,
# Telegram-бота и служебные таймеры. От тебя нужен только токен бота —
# его можно вставить позже одной командой `sudo doss-set-token`.
#
#   sudo ./deploy/install-all.sh              # всё, токен спросим/оставим на потом
#   sudo ./deploy/install-all.sh --token 123:ABC   # сразу с токеном
#   sudo ./deploy/install-all.sh --dry-run    # показать план, ничего не менять
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOSS_DIR="$(cd "$HERE/.." && pwd)"          # .../doss

TOKEN="${DOSS_BOT_TOKEN:-}"
DEST="www.microsoft.com"
WITH_XHTTP="1"
DRY="0"
BRAND="Doss"
SKIP_SERVER="0"
SKIP_APPS="0"

log()  { printf '\033[1;36m[doss]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[doss]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[doss] ОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [[ "$DRY" == "1" ]]; then printf '  (dry-run) %s\n' "$*"; else "$@"; fi; }

usage() { sed -n '2,16p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)      TOKEN="${2:?}"; shift 2 ;;
    --dest)       DEST="${2:?}"; shift 2 ;;
    --no-xhttp)   WITH_XHTTP="0"; shift ;;
    --dry-run)    DRY="1"; shift ;;
    --skip-server) SKIP_SERVER="1"; shift ;;
    --skip-apps)  SKIP_APPS="1"; shift ;;
    -h|--help)    usage ;;
    *) die "неизвестная опция: $1 (см. --help)" ;;
  esac
done

[[ "$DRY" == "1" || "$(id -u)" -eq 0 || "${DOSS_ALLOW_NONROOT:-0}" == "1" ]] || die "запускай от root: sudo $0"
[[ -d "$DOSS_DIR/server" && -f "$DOSS_DIR/server/install.sh" ]] \
  || die "запускай из каталога репозитория doss/ (нет server/install.sh)"

echo
log "Doss: установка «под ключ»  (каталог: $DOSS_DIR)"
echo "──────────────────────────────────────────────────────────────"

# ---------------------------------------------------------------- 1. сервер
if [[ "$SKIP_SERVER" == "1" ]]; then
  warn "шаг 1 (VPN-узел) пропущен по флагу"
else
  log "1/4 · VPN-узел: Xray + VLESS/REALITY$([[ "$WITH_XHTTP" == "1" ]] && echo ' + XHTTP')"
  args=(--dest "$DEST" --owner owner --yes)
  [[ "$WITH_XHTTP" == "1" ]] && args+=(--with-xhttp)
  if [[ "$DRY" == "1" ]]; then
    printf '  (dry-run) bash %s/server/install.sh %s\n' "$DOSS_DIR" "${args[*]}"
  else
    bash "$DOSS_DIR/server/install.sh" "${args[@]}"
  fi
  ok "узел установлен"
fi

# ----------------------------------------------------------------- 2. приложения
if [[ "$SKIP_APPS" == "1" ]]; then
  warn "шаг 2 (панель и бот) пропущен по флагу"
else
  log "2/4 · панель + бот + служебные sudo-обёртки"
  run bash "$DOSS_DIR/deploy/bootstrap-apps.sh" --repo "$DOSS_DIR"
  ok "панель и бот установлены"

  # провайдер по умолчанию: локальный, через ограниченную sudo-обёртку
  if [[ "$DRY" == "0" && -f /etc/doss/bot.env ]]; then
    sed -i 's#^PROVIDER=.*#PROVIDER=local#' /etc/doss/bot.env
    sed -i 's#^DOSS_BIN=.*#DOSS_BIN=sudo /usr/local/bin/doss-run#' /etc/doss/bot.env
    sed -i 's#^OWNER_ID=.*#OWNER_ID=#' /etc/doss/bot.env
  fi
  ok "бот настроен на локальный CLI (owner определится при первом /start)"
fi

# ------------------------------------------------------------------- 3. токен
log "3/4 · токен Telegram-бота"
if [[ -n "$TOKEN" ]]; then
  if [[ "$DRY" == "1" ]]; then
    printf '  (dry-run) doss-set-token <token>\n'
  else
    install -m 0755 "$DOSS_DIR/deploy/doss-set-token" /usr/local/bin/doss-set-token
    /usr/local/bin/doss-set-token "$TOKEN" || warn "токен не принят — вставь его позже: sudo doss-set-token"
  fi
else
  run install -m 0755 "$DOSS_DIR/deploy/doss-set-token" /usr/local/bin/doss-set-token
  warn "токена нет — это единственный шаг, который остался тебе."
  echo "     Когда будет токен у @BotFather, выполни на сервере:"
  echo "       sudo doss-set-token"
  echo "     (вставишь токен — скрипт сам проверит его, запустит бота и покажет ссылку)"
fi

# ------------------------------------------------------------------ 4. проверка
log "4/4 · самопроверка"
if [[ "$DRY" == "1" ]]; then
  printf '  (dry-run) doss-doctor\n'
else
  install -m 0755 "$DOSS_DIR/deploy/doss-doctor" /usr/local/bin/doss-doctor
  doss-doctor || warn "часть проверок не прошла — смотри вывод выше"
fi

bot_state() {
  local env=/etc/doss/bot.env tok
  [[ -f "$env" ]] || { echo "не установлен"; return; }
  tok="$(grep -h '^BOT_TOKEN=' "$env" | cut -d= -f2- || true)"
  if [[ -z "$tok" || "$tok" == "CHANGE_ME" ]]; then
    echo "ждёт токена → sudo doss-set-token"
  else
    echo "настроен (проверь: sudo doss-doctor)"
  fi
}

echo
echo "──────────────────────────────────────────────────────────────"
log "Готово. Что теперь есть:"
echo "  • VPN-узел      : $( [[ -f /etc/doss/state.json ]] && jq -r '.server.host + ":" + (.server.port|tostring)' /etc/doss/state.json 2>/dev/null || echo '<после установки>')"
echo "  • Панель        : http://127.0.0.1:8080 (только локально, наружу не торчит)"
echo "  • Токен админки : $(grep -h '^ADMIN_TOKEN=' /etc/doss/panel.env 2>/dev/null | cut -d= -f2 || echo '—')"
echo "  • Бот           : $(bot_state)"
echo
echo "Тебе осталось (2 минуты):"
echo "  1) @BotFather → /newbot → скопировать токен"
echo "  2) на сервере: sudo doss-set-token   → вставить токен"
echo "  3) открыть бота, нажать /start — ты автоматически станешь владельцем"
echo "  4) себе безлимит: /grant <твой tg_id из /whoami> inf"
echo
echo "Проверить состояние в любой момент: sudo doss-doctor"
echo "──────────────────────────────────────────────────────────────"
