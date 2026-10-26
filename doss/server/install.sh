#!/usr/bin/env bash
#
# Doss — серверный установщик (Ubuntu 22.04/24.04, Debian 12; x86_64 и arm64)
#
#   curl -fsSL https://<твой-репозиторий>/server/install.sh | bash -s -- --dest www.microsoft.com
#   или локально:  sudo ./install.sh --dest www.microsoft.com
#
# Что делает:
#   1. ставит Xray и поднимает VLESS + REALITY (+ опционально XHTTP) на 443/tcp
#   2. генерирует ключи REALITY и первого пользователя
#   3. чинит файрвол (Oracle-образы блокируют всё, кроме 22)
#   4. включает BBR и, по желанию, «анти-idle» для Oracle Always Free
#   5. ставит CLI `doss` для выдачи/отзыва аккаунтов
#
# Скрипт идемпотентный: повторный запуск не ломает существующих пользователей.

set -euo pipefail

DOSS_VERSION="0.1.0"
DOSS_ETC="${DOSS_ETC:-/etc/doss}"
XRAY_CONFIG_DIR="${XRAY_CONFIG_DIR:-/usr/local/etc/xray}"
XRAY_CONFIG="${XRAY_CONFIG:-$XRAY_CONFIG_DIR/config.json}"
XRAY_BIN="${XRAY_BIN:-/usr/local/bin/xray}"
DOSS_BIN="/usr/local/bin/doss"
SUB_DIR="${SUB_DIR:-/var/www/doss-sub}"

DEST_DOMAIN="www.microsoft.com"
PORT="443"
WITH_XHTTP="0"
XHTTP_PORT="8443"
KEEPALIVE="1"
OWNER_NAME="owner"
ASSUME_YES="0"

log()  { printf '\033[1;36m[doss]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[doss]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[doss] ОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Использование: install.sh [опции]

  --dest <domain>      домен-маска для REALITY (по умолчанию www.microsoft.com)
  --port <n>           TCP-порт Xray (по умолчанию 443)
  --with-xhttp         добавить второй инбаунд VLESS+XHTTP на --xhttp-port
  --xhttp-port <n>     порт XHTTP (по умолчанию 8443)
  --no-keepalive       не ставить анти-idle таймер (нужен только на Oracle Free)
  --owner <name>       имя первого пользователя (по умолчанию owner)
  -y, --yes            без вопросов
  -h, --help           эта справка
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)         DEST_DOMAIN="${2:?}"; shift 2 ;;
    --port)         PORT="${2:?}"; shift 2 ;;
    --with-xhttp)   WITH_XHTTP="1"; shift ;;
    --xhttp-port)   XHTTP_PORT="${2:?}"; shift 2 ;;
    --no-keepalive) KEEPALIVE="0"; shift ;;
    --owner)        OWNER_NAME="${2:?}"; shift 2 ;;
    -y|--yes)       ASSUME_YES="1"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) die "неизвестная опция: $1 (см. --help)" ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || die "запускай от root: sudo $0"

# ---------------------------------------------------------------- 0. система
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) : ;;
    *) warn "дистрибутив ${ID:-?} не проверялся — продолжаю на свой риск" ;;
  esac
else
  die "не могу определить ОС (/etc/os-release)"
fi

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64|amd64)  XRAY_ARCH="64" ;;
  aarch64|arm64) XRAY_ARCH="arm64-v8a" ;;
  armv7l)        XRAY_ARCH="arm32-v7a" ;;
  *) die "неизвестная архитектура: $ARCH_RAW" ;;
esac
log "ОС: ${PRETTY_NAME:-?}, архитектура: $ARCH_RAW"

export DEBIAN_FRONTEND=noninteractive
APT_PKGS=(curl jq unzip ca-certificates openssl iproute2 iptables python3 qrencode)
log "ставлю зависимости: ${APT_PKGS[*]}"
apt-get update -qq
# qrencode не критичен: если пакета нет — не падаем
apt-get install -y -qq "${APT_PKGS[@]}" >/dev/null 2>&1 || {
  apt-get install -y -qq curl jq unzip ca-certificates openssl iproute2 iptables python3 >/dev/null
  warn "qrencode не установился — QR-коды будут недоступны (ссылки работают)"
}

command -v jq >/dev/null || die "jq обязателен"
command -v openssl >/dev/null || die "openssl обязателен"

# ---------------------------------------------------------------- 1. Xray
install_xray() {
  if [[ -x "$XRAY_BIN" ]] && "$XRAY_BIN" version >/dev/null 2>&1; then
    log "Xray уже установлен: $("$XRAY_BIN" version | head -1)"
    return
  fi

  log "ставлю Xray-core официальным установщиком"
  if curl -fsSL --max-time 60 https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/xray-install.sh \
     && bash /tmp/xray-install.sh install >/dev/null 2>&1; then
    log "Xray установлен через install-release.sh"
    return
  fi

  warn "официальный установщик не сработал — качаю релиз напрямую"
  local tmp; tmp="$(mktemp -d)"
  curl -fsSL --max-time 120 -o "$tmp/xray.zip" \
    "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-${XRAY_ARCH}.zip" \
    || die "не смог скачать Xray ($XRAY_ARCH)"
  unzip -oq "$tmp/xray.zip" -d "$tmp"
  install -m 0755 "$tmp/xray" "$XRAY_BIN"
  mkdir -p /usr/local/share/xray
  install -m 0644 "$tmp/geoip.dat" /usr/local/share/xray/geoip.dat 2>/dev/null || true
  install -m 0644 "$tmp/geosite.dat" /usr/local/share/xray/geosite.dat 2>/dev/null || true
  rm -rf "$tmp"
  command -v "$XRAY_BIN" >/dev/null || die "Xray не установился"
  log "Xray установлен вручную: $("$XRAY_BIN" version | head -1)"
}

install_xray
XRAY_VER_LINE="$("$XRAY_BIN" version | head -1)"
log "версия: $XRAY_VER_LINE"

# ------------------------------------------------------- 2. ключи и состояние
mkdir -p "$DOSS_ETC" "$XRAY_CONFIG_DIR" "$SUB_DIR"
chmod 700 "$DOSS_ETC"

STATE="$DOSS_ETC/state.json"

gen_reality_keys() {
  # Xray 24.9+ печатает: PrivateKey / Password (=public) / Hash32.
  # Старые версии: Private key / Public key. Разбираем оба варианта.
  local out priv pub
  out="$("$XRAY_BIN" x25519)"
  priv="$(printf '%s\n' "$out" | awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}')"
  pub="$(printf '%s\n' "$out"  | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')"
  [[ -n "$priv" && -n "$pub" ]] || die "не смог разобрать вывод 'xray x25519'. Вывод: $out"
  printf '%s\n%s\n' "$priv" "$pub"
}

random_hex() { openssl rand -hex "${1:-8}"; }

if [[ -f "$STATE" ]]; then
  log "нашёл существующее состояние: $STATE (ключи сохраняю)"
  PUB_KEY="$(jq -r '.server.public_key' "$STATE")"
  PRIV_KEY="$(jq -r '.server.private_key' "$STATE")"
  SHORT_IDS="$(jq -c '.server.short_ids' "$STATE")"
  # применяем новые параметры, если их передали
  jq --arg dest "$DEST_DOMAIN" --argjson port "$PORT" \
     --argjson xhttp "$([[ "$WITH_XHTTP" == 1 ]] && echo true || echo false)" \
     --argjson xport "$XHTTP_PORT" \
     '.server.dest_domain = $dest
      | .server.port = $port
      | .server.xhttp.enabled = $xhttp
      | .server.xhttp.port = $xport' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
else
  log "генерирую ключи REALITY"
  { read -r PRIV_KEY; read -r PUB_KEY; } < <(gen_reality_keys)
  SHORT_IDS="$(jq -cn --arg a "$(random_hex 4)" --arg b "$(random_hex 8)" '["", $a, $b]')"

  HOST_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  [[ -z "$HOST_IP" ]] && HOST_IP="$(curl -fsS --max-time 10 https://ifconfig.me || true)"
  [[ -z "$HOST_IP" ]] && HOST_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"

  jq -n \
    --arg created "$(date -u +%FT%TZ)" \
    --arg host "$HOST_IP" \
    --arg dest "$DEST_DOMAIN" \
    --argjson port "$PORT" \
    --arg priv "$PRIV_KEY" --arg pub "$PUB_KEY" \
    --argjson sids "$SHORT_IDS" \
    --argjson xhttp "$([[ "$WITH_XHTTP" == 1 ]] && echo true || echo false)" \
    --argjson xport "$XHTTP_PORT" \
    '{ version: 1,
       created: $created,
       server: {
         host: $host, port: $port, dest_domain: $dest,
         private_key: $priv, public_key: $pub, short_ids: $sids,
         xhttp: { enabled: $xhttp, port: $xport, path: "/doss-xh" },
         awg: { enabled: false }
       },
       users: {} }' > "$STATE"
  chmod 600 "$STATE"
fi

log "маска REALITY: $DEST_DOMAIN, публичный ключ: $PUB_KEY"

# ------------------------------------- 3. (см. `doss build-base` ниже)
# Шаблон конфига Xray генерирует сам CLI из state.json — одна точка правды,
# без дублирования JSON между установщиком и утилитой.

# ------------------------------------------------------- 4. CLI doss
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SRC_DIR/doss" ]]; then
  install -m 0755 "$SRC_DIR/doss" "$DOSS_BIN"
else
  die "рядом с install.sh нет файла 'doss' — скачай репозиторий целиком, не один скрипт"
fi
log "CLI установлен: $DOSS_BIN"

"$DOSS_BIN" build-base || die "не удалось собрать шаблон конфига"

# ------------------------------------------------------- 5. systemd + запуск
mkdir -p "$XRAY_CONFIG_DIR"

cat > /etc/systemd/system/xray.service <<EOF
[Unit]
Description=Xray Service (Doss)
Documentation=https://github.com/XTLS/Xray-core
After=network.target nss-lookup.target

[Service]
User=root
Environment=XRAY_LOCATION_ASSET=/usr/local/share/xray
ExecStart=$XRAY_BIN run -config $XRAY_CONFIG
Restart=on-failure
RestartSec=5
LimitNOFILE=1000000

[Install]
WantedBy=multi-user.target
EOF

# первый пользователь
if [[ "$(jq '.users | length' "$STATE")" == "0" ]]; then
  "$DOSS_BIN" user add "$OWNER_NAME" --unlimited --note "владелец" >/dev/null
  log "создан пользователь '$OWNER_NAME' (unlimited)"
else
  "$DOSS_BIN" rebuild
fi

systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 1
systemctl is-active --quiet xray || die "Xray не запустился: journalctl -u xray -n 50 --no-pager"

# ------------------------------------------------------- 6. сеть, BBR, фаервол
cat > /etc/sysctl.d/99-doss.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_fastopen = 3
net.ipv4.ip_forward = 0
EOF
sysctl --system >/dev/null 2>&1 || warn "sysctl не применился целиком"

open_port() {
  local proto="$1" port="$2"

  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$port/$proto" >/dev/null 2>&1 || true
    return
  fi

  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$port/$proto" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    return
  fi

  # Oracle/Alma-образы: iptables с финальным REJECT в INPUT.
  local ipt6=()
  ip6tables -L >/dev/null 2>&1 && ipt6=(ip6tables)

  if iptables -S INPUT 2>/dev/null | grep -qE '^-A INPUT -j REJECT|^-A INPUT -j DROP'; then
    if ! iptables -C INPUT -p "$proto" --dport "$port" -j ACCEPT 2>/dev/null; then
      # вставляем перед правилом REJECT/DROP
      local rule_num
      rule_num="$(iptables -L INPUT --line-numbers -n | awk '/REJECT|DROP/{print $1; exit}')"
      iptables -I INPUT "${rule_num:-1}" -p "$proto" --dport "$port" -j ACCEPT 2>/dev/null || true
    fi
    for t in "${ipt6[@]:-}"; do
      [[ -z "$t" ]] && continue
      if "$t" -S INPUT 2>/dev/null | grep -qE '^-A INPUT -j (REJECT|DROP)'; then
        local r6
        r6="$("$t" -L INPUT --line-numbers -n | awk '/REJECT|DROP/{print $1; exit}')"
        "$t" -C INPUT -p "$proto" --dport "$port" -j ACCEPT 2>/dev/null \
          || "$t" -I INPUT "${r6:-1}" -p "$proto" --dport "$port" -j ACCEPT 2>/dev/null || true
      fi
    done
    command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
  fi
}

open_port tcp "$PORT"
[[ "$WITH_XHTTP" == "1" ]] && open_port tcp "$XHTTP_PORT"
log "порт $PORT/tcp открыт локально (не забудь Security List / Cloud Firewall у провайдера!)"

# ------------------------------------------------------- 7. анти-idle (Oracle)
if [[ "$KEEPALIVE" == "1" ]]; then
  cat > /usr/local/bin/doss-keepalive <<'EOF'
#!/usr/bin/env bash
# Oracle Always Free забирает инстанс, если 7 дней подряд CPU/сеть/память < 20 %.
# Держим лёгкий, но заметный фон: короткий CPU-бёрн + 8 МБ трафика в час.
set -euo pipefail
timeout 45 openssl speed -seconds 20 -evp sha256 >/dev/null 2>&1 || true
dd if=/dev/urandom of=/dev/null bs=1M count=64 status=none || true
curl -fsS --max-time 20 -o /dev/null https://speed.cloudflare.com/__down?bytes=8000000 || true
python3 - <<'PY' 2>/dev/null || true
import time
# держим ~15 % одного ядра
end = time.time() + 20
x = 1
while time.time() < end:
    for _ in range(200000):
        x = (x * 1103515245 + 12345) % 2147483648
PY
EOF
  chmod 0755 /usr/local/bin/doss-keepalive

  cat > /etc/systemd/system/doss-keepalive.service <<'EOF'
[Unit]
Description=Doss anti-idle keepalive (Oracle Always Free)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/doss-keepalive
EOF
  cat > /etc/systemd/system/doss-keepalive.timer <<'EOF'
[Unit]
Description=Doss anti-idle keepalive timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
AccuracySec=1min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now doss-keepalive.timer >/dev/null 2>&1 || warn "таймер keepalive не включился"
  log "анти-idle таймер включён (каждые 30 мин, ~8 МБ трафика)"
fi

# ------------------------------------------------------- 8. ежедневный tick
cat > /etc/systemd/system/doss-tick.service <<EOF
[Unit]
Description=Doss daily maintenance (истечение подписок)
[Service]
Type=oneshot
ExecStart=$DOSS_BIN tick
EOF
cat > /etc/systemd/system/doss-tick.timer <<'EOF'
[Unit]
Description=Doss daily maintenance timer
[Timer]
OnCalendar=*-*-* 04:15:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now doss-tick.timer >/dev/null 2>&1 || true

# ------------------------------------------------------- 9. итог
HOST="$(jq -r '.server.host' "$STATE")"
echo
log "готово. Версия Doss $DOSS_VERSION"
echo "──────────────────────────────────────────────────────────────"
"$DOSS_BIN" user list || true
echo "──────────────────────────────────────────────────────────────"
"$DOSS_BIN" link "$OWNER_NAME" || true
echo "──────────────────────────────────────────────────────────────"
cat <<EOF

Дальше:
  doss user add <имя> [--expires 2026-12-31] [--traffic 200]   — новый аккаунт
  doss link <имя>                                              — ссылка vless://
  doss qr <имя>                                                — QR в терминале
  doss sub <имя> --url-base http://$HOST                     — ссылка-подписка
  doss user rm <имя>                                           — отозвать

Проверка снаружи (с другого хоста):
  nc -vz $HOST $PORT
  curl -sk --max-time 5 https://$HOST/ -o /dev/null -w '%{http_code}\\n'

Не забудь открыть $PORT/tcp в Security List (Oracle) / Firewall (прочее).
EOF
