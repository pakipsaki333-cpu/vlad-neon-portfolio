#!/usr/bin/env bash
#
# bootstrap-apps.sh — ставит панель и бота на тот же сервер, где работает doss.
#
#   sudo ./bootstrap-apps.sh --repo /path/to/doss --domain vpn.example.com
#
# Делает:
#   1. пользователя doss (без shell-логина), /opt/doss/{panel,bot}
#   2. venv + зависимости
#   3. SSH-ключ для бота и обёртку, чтобы по нему можно было вызвать ТОЛЬКО doss
#   4. env-файлы (если их нет), systemd-юниты
#   5. печатает, что осталось сделать руками (BOT_TOKEN, DNS, certbot)
#
set -euo pipefail

REPO=""; DOMAIN=""; ENABLE_PANEL="1"; ENABLE_BOT="1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:?}"; shift 2 ;;
    --domain) DOMAIN="${2:?}"; shift 2 ;;
    --no-panel) ENABLE_PANEL="0"; shift ;;
    --no-bot) ENABLE_BOT="0"; shift ;;
    *) echo "неизвестная опция: $1" >&2; exit 1 ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "нужен root" >&2; exit 1; }
[[ -n "$REPO" ]] || { echo "--repo обязателен" >&2; exit 1; }
[[ -d "$REPO/panel" && -d "$REPO/bot" ]] || { echo "в $REPO нет panel/ или bot/" >&2; exit 1; }

log() { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }

id -u doss >/dev/null 2>&1 || {
  log "создаю пользователя doss"
  useradd --system --create-home --shell /usr/sbin/nologin doss
}
# панель должна уметь читать состояние и вызывать CLI
usermod -aG doss doss
chmod 750 /etc/doss 2>/dev/null || true

mkdir -p /opt/doss
rm -rf /opt/doss/panel /opt/doss/bot
cp -r "$REPO/panel" /opt/doss/panel
cp -r "$REPO/bot" /opt/doss/bot
rm -rf /opt/doss/panel/__pycache__ /opt/doss/bot/__pycache__ /opt/doss/bot/tests

log "ставлю python-зависимости"
for app in panel bot; do
  [[ -d "/opt/doss/$app" ]] || continue
  python3 -m venv "/opt/doss/$app/.venv"
  "/opt/doss/$app/.venv/bin/pip" install --quiet --upgrade pip
  "/opt/doss/$app/.venv/bin/pip" install --quiet -r "/opt/doss/$app/requirements.txt"
done

# --- ключ, которым бот вызывает CLI
KEY=/etc/doss/bot_id_ed25519
if [[ ! -f "$KEY" ]]; then
  log "генерирую SSH-ключ для бота"
  ssh-keygen -t ed25519 -N "" -C "doss-bot" -f "$KEY" >/dev/null
fi
chmod 600 "$KEY"; chmod 644 "$KEY.pub"
chown -R doss:doss /opt/doss "$KEY"*

# обёртка: по ключу бота можно вызвать только `doss`
install -m 0755 "$REPO/deploy/doss-ssh" /usr/local/bin/doss-ssh
install -m 0755 "$REPO/deploy/doss-run" /usr/local/bin/doss-run

AUTH=/root/.ssh/authorized_keys
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch "$AUTH"
if ! grep -q "doss-bot" "$AUTH"; then
  {
    echo -n 'command="sudo /usr/local/bin/doss-ssh",no-port-forwarding,no-agent-forwarding,no-pty '
    cat "$KEY.pub"
  } >> "$AUTH"
  chmod 600 "$AUTH"
fi
if [[ ! -f /etc/sudoers.d/doss-bot ]]; then
  cat > /etc/sudoers.d/doss-bot <<'SUDO'
doss ALL=(root) NOPASSWD: /usr/local/bin/doss-ssh, /usr/local/bin/doss-run
SUDO
  chmod 440 /etc/sudoers.d/doss-bot
  visudo -cf /etc/sudoers.d/doss-bot >/dev/null && log "разрешил doss вызывать только doss-ssh и doss-run"
fi

# --- env-файлы
mkdir -p /etc/doss
if [[ ! -f /etc/doss/panel.env ]]; then
  cat > /etc/doss/panel.env <<EOF
DOSS_ETC=/etc/doss
DOSS_BIN=sudo /usr/local/bin/doss-run
ADMIN_TOKEN=$(openssl rand -hex 24)
SUB_BASE=https://${DOMAIN:-vpn.example.com}
BRAND=Doss
EOF
  chmod 640 /etc/doss/panel.env
  chown root:doss /etc/doss/panel.env
  log "ADMIN_TOKEN : $(grep ADMIN_TOKEN /etc/doss/panel.env | cut -d= -f2)"
fi

if [[ ! -f /etc/doss/bot.env ]]; then
  cat > /etc/doss/bot.env <<EOF
BOT_TOKEN=CHANGE_ME
OWNER_ID=CHANGE_ME
PROVIDER=ssh
SSH_HOST=127.0.0.1
SSH_USER=doss
SSH_KEY=$KEY
DOSS_BIN=doss
DB_PATH=/var/lib/doss/bot.sqlite3
TRIAL_DAYS=3
TRIAL_GB=10
PLAN_DAYS=30
PLAN_GB=200
STARS_PRICE=150
SUB_BASE=https://${DOMAIN:-vpn.example.com}
EOF
  chmod 640 /etc/doss/bot.env
  chown root:doss /etc/doss/bot.env
fi
mkdir -p /var/lib/doss && chown doss:doss /var/lib/doss

# --- systemd
install -m 0644 "$REPO/deploy/doss-panel.service" /etc/systemd/system/ 2>/dev/null || true
install -m 0644 "$REPO/deploy/doss-bot.service" /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload

if [[ "$ENABLE_PANEL" == "1" && -f /etc/systemd/system/doss-panel.service ]]; then
  systemctl enable --now doss-panel >/dev/null 2>&1 || log "панель не запустилась — смотри journalctl -u doss-panel"
fi
if [[ "$ENABLE_BOT" == "1" && -f /etc/systemd/system/doss-bot.service ]]; then
  if grep -q CHANGE_ME /etc/doss/bot.env; then
    log "бот установлен, но BOT_TOKEN/OWNER_ID не заданы — заполни /etc/doss/bot.env и: systemctl enable --now doss-bot"
  else
    systemctl enable --now doss-bot >/dev/null 2>&1 || log "бот не запустился — journalctl -u doss-bot"
  fi
fi

log "готово. Дальше:"
cat <<EOF
  1) A-запись ${DOMAIN:-vpn.example.com} → этот сервер
  2) apt install nginx certbot python3-certbot-nginx
     cp deploy/nginx-doss.conf /etc/nginx/sites-available/doss && ln -s ... && certbot --nginx -d ${DOMAIN:-vpn.example.com}
  3) /etc/doss/bot.env: BOT_TOKEN (у @BotFather), OWNER_ID (@userinfobot)
  4) systemctl restart doss-bot doss-panel
  5) открыть https://${DOMAIN:-vpn.example.com}/admin?key=<ADMIN_TOKEN>
EOF
