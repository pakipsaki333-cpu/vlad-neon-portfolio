#!/usr/bin/env bash
#
# awg-setup.sh — ДОПОЛНИТЕЛЬНЫЙ транспорт AmneziaWG для Doss.
#
# Зачем: Xray/VLESS закрывает 95 % случаев. AmneziaWG добавляет второй,
# «UDP-быстрый» путь (хорош на домашнем интернете и как резерв, когда TCP
# душат). В мобильных сетях РФ AWG проходит не везде — см. docs/protocols.md.
#
# ВАЖНО: этот модуль НЕ проверялся в песочнице (там нельзя собрать
# kernel-модуль). Первый запуск делай на тестовом сервере.
#
# Использование:
#   sudo ./awg-setup.sh install [--port 51820] [--subnet 10.66.66]
#   sudo ./awg-setup.sh peer anna          # создать пира, выдать конфиг+QR
#   sudo ./awg-setup.sh list
#   sudo ./awg-setup.sh rm anna
#   sudo ./awg-setup.sh show
#
set -uo pipefail

AWG_DIR="/etc/doss/awg"
IFACE="awg0"
CONF="/etc/amnezia/amneziawg/${IFACE}.conf"
PORT="51820"
SUBNET="10.66.66"
MTU="1280"
PEERS_JSON="$AWG_DIR/peers.json"

log()  { printf '\033[1;36m[awg]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[awg]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[awg]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "запускай от root"

need_jq() { command -v jq >/dev/null || { apt-get update -qq && apt-get install -y -qq jq; }; }

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq

  if command -v awg >/dev/null && command -v awg-quick >/dev/null; then
    log "AmneziaWG tools уже установлены"
    return
  fi

  log "пробую PPA Amnezia (Ubuntu 22.04/24.04)"
  apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
  if add-apt-repository -y ppa:amnezia/ppa >/dev/null 2>&1; then
    apt-get update -qq || true
    apt-get install -y -qq amneziawg amneziawg-tools >/dev/null 2>&1 \
      || apt-get install -y -qq amneziawg-tools >/dev/null 2>&1 \
      || true
  fi

  if command -v awg >/dev/null && command -v awg-quick >/dev/null; then
    log "awg-tools установлены из PPA"
    return
  fi

  warn "PPA не сработал (на arm64 сборок может не быть)."
  warn "Собери из исходников:"
  cat <<'EOF'
    apt-get install -y golang-go make git libmnl-dev libelf-dev build-essential \
        linux-headers-$(uname -r)
    # userspace-демон (работает без kernel-модуля)
    git clone https://github.com/amnezia-vpn/amneziawg-go /opt/amneziawg-go
    make -C /opt/amneziawg-go && make -C /opt/amneziawg-go install
    # утилиты awg/awg-quick
    git clone https://github.com/amnezia-vpn/amneziawg-tools /opt/amneziawg-tools
    make -C /opt/amneziawg-tools/src && make -C /opt/amneziawg-tools/src install
EOF
  die "поставь пакеты вручную по инструкции выше и запусти install снова"
}

rand32() { printf '%u' $(( (RANDOM << 15 | RANDOM) % 2147483647 + 1 )); }

# Обфускация: значения в духе дефолтов Amnezia. Каждый сервер — свой набор,
# это и есть отпечаток, по которому DPI не может опознать WireGuard.
gen_obfuscation() {
  OB_JC=5
  OB_JMIN=50
  OB_JMAX=1000
  OB_S1=86
  OB_S2=574
  OB_H1="$(rand32)"
  OB_H2="$(rand32)"
  OB_H3="$(rand32)"
  OB_H4="$(rand32)"
}

# Полный конфиг интерфейса = [Interface] + пиры из peers.json.
# Источник правды — peers.json, поэтому добавление/удаление идемпотентно.
render_conf() {
  mkdir -p "$(dirname "$CONF")"
  local tmp; tmp="$(mktemp)"
  cat "$AWG_DIR/interface.conf" > "$tmp"
  jq -r '.peers | to_entries[] | "\n[Peer]\n# \(.key)\nPublicKey = \(.value.public_key)\nAllowedIPs = \(.value.ip)/32"' \
     "$PEERS_JSON" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$CONF"
}

cmd_install() {
  need_jq
  install_packages

  local iface_default
  iface_default="$(ip -4 route show default | awk '{print $5; exit}')"
  [[ -n "$iface_default" ]] || die "не нашёл внешний интерфейс"

  mkdir -p "$AWG_DIR" /etc/amnezia/amneziawg
  chmod 700 "$AWG_DIR"

  if [[ -f "$AWG_DIR/server.key" ]]; then
    log "серверный ключ уже есть — оставляю"
  else
    umask 077
    awg genkey > "$AWG_DIR/server.key"
    awg pubkey < "$AWG_DIR/server.key" > "$AWG_DIR/server.pub"
  fi

  # Параметры обфускации переиспользуем, чтобы пиры не «поехали»
  if [[ -f "$AWG_DIR/obf.env" ]]; then
    # shellcheck disable=SC1091
    . "$AWG_DIR/obf.env"
  else
    gen_obfuscation
    umask 077
    {
      echo "OB_JC=$OB_JC"; echo "OB_JMIN=$OB_JMIN"; echo "OB_JMAX=$OB_JMAX"
      echo "OB_S1=$OB_S1"; echo "OB_S2=$OB_S2"
      echo "OB_H1=$OB_H1"; echo "OB_H2=$OB_H2"; echo "OB_H3=$OB_H3"; echo "OB_H4=$OB_H4"
    } > "$AWG_DIR/obf.env"
  fi

  if [[ ! -f "$PEERS_JSON" ]]; then
    echo '{"peers":{}}' > "$PEERS_JSON"
    chmod 600 "$PEERS_JSON"
  fi

  if [[ ! -f "$AWG_DIR/interface.conf" ]]; then
    umask 077
    cat > "$AWG_DIR/interface.conf" <<EOF
[Interface]
PrivateKey = $(cat "$AWG_DIR/server.key")
Address = ${SUBNET}.1/24
ListenPort = $PORT
MTU = $MTU
Jc = $OB_JC
Jmin = $OB_JMIN
Jmax = $OB_JMAX
S1 = $OB_S1
S2 = $OB_S2
H1 = $OB_H1
H2 = $OB_H2
H3 = $OB_H3
H4 = $OB_H4
PostUp   = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -s ${SUBNET}.0/24 -o ${iface_default} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -s ${SUBNET}.0/24 -o ${iface_default} -j MASQUERADE
EOF
    chmod 600 "$AWG_DIR/interface.conf"
  fi

  render_conf

  # форвардинг + открыть UDP-порт наружу
  cat > /etc/sysctl.d/99-doss-awg.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
  sysctl --system >/dev/null 2>&1 || warn "sysctl не применился"

  iptables -C INPUT -p udp --dport "$PORT" -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 1 -p udp --dport "$PORT" -j ACCEPT || warn "не смог открыть UDP $PORT в iptables"

  systemctl enable "awg-quick@${IFACE}" >/dev/null 2>&1 || true
  systemctl restart "awg-quick@${IFACE}" || die "не удалось поднять $IFACE: journalctl -u awg-quick@${IFACE} -n 40"
  sleep 1
  systemctl is-active --quiet "awg-quick@${IFACE}" || die "интерфейс не поднялся"

  log "интерфейс $IFACE поднят, порт $PORT/udp"
  log "открой $PORT/udp в Security List провайдера!"
  awg show || true
}

cmd_peer() {
  need_jq
  local name="${1:-}"; shift || true
  [[ -n "$name" ]] || die "использование: awg-setup.sh peer <имя> [--keepalive]"
  [[ -f "$CONF" ]] || die "сначала запусти install"
  jq -e --arg n "$name" '.peers[$n] == null' "$PEERS_JSON" >/dev/null || die "пир '$name' уже есть"

  # shellcheck disable=SC1091
  . "$AWG_DIR/obf.env"

  local next_ip priv pub endpoint
  next_ip="$(jq -r '[.peers[].ip | split(".")[3] | tonumber] | (max // 1) + 1' "$PEERS_JSON")"
  [[ "$next_ip" -lt 2 ]] && next_ip=2
  [[ "$next_ip" -gt 254 ]] && die "адреса в подсети закончились"

  priv="$(awg genkey)"
  pub="$(printf '%s' "$priv" | awg pubkey)"
  endpoint="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
  [[ -z "$endpoint" ]] && endpoint="$(curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true)"
  [[ -z "$endpoint" ]] && endpoint="<IP_СЕРВЕРА>"

  jq --arg n "$name" --arg ip "${SUBNET}.${next_ip}" --arg pub "$pub" \
     '.peers[$n] = {ip:$ip, public_key:$pub, created: (now|todate)}' "$PEERS_JSON" \
     > "$PEERS_JSON.tmp" && mv "$PEERS_JSON.tmp" "$PEERS_JSON"
  render_conf
  systemctl restart "awg-quick@${IFACE}" || die "не удалось применить конфиг"

  mkdir -p "$AWG_DIR/peers"
  local out="$AWG_DIR/peers/${name}.conf"
  umask 077
  cat > "$out" <<EOF
[Interface]
PrivateKey = $priv
Address = ${SUBNET}.${next_ip}/32
DNS = 1.1.1.1, 8.8.8.8
MTU = $MTU
Jc = $OB_JC
Jmin = $OB_JMIN
Jmax = $OB_JMAX
S1 = $OB_S1
S2 = $OB_S2
H1 = $OB_H1
H2 = $OB_H2
H3 = $OB_H3
H4 = $OB_H4

[Peer]
PublicKey = $(cat "$AWG_DIR/server.pub")
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = ${endpoint}:${PORT}
PersistentKeepalive = 25
EOF
  chmod 600 "$out"

  log "пир '$name' создан (${SUBNET}.${next_ip}), конфиг: $out"
  if command -v qrencode >/dev/null; then qrencode -t ANSIUTF8 < "$out" || true; fi
  cat "$out"
}

cmd_list() {
  need_jq
  jq -r '.peers | to_entries[] | "\(.key)\t\(.value.ip)\t\(.value.created)"' "$PEERS_JSON" \
    | column -t 2>/dev/null || jq . "$PEERS_JSON"
}

cmd_rm() {
  need_jq
  local name="${1:?укажи имя}"
  local pub
  pub="$(jq -r --arg n "$name" '.peers[$n].public_key // empty' "$PEERS_JSON")"
  [[ -n "$pub" ]] || die "нет пира $name"
  jq --arg n "$name" 'del(.peers[$n])' "$PEERS_JSON" > "$PEERS_JSON.tmp" && mv "$PEERS_JSON.tmp" "$PEERS_JSON"
  render_conf
  systemctl restart "awg-quick@${IFACE}" || warn "перезапуск интерфейса не удался — проверь вручную"
  rm -f "$AWG_DIR/peers/${name}.conf"
  log "пир '$name' удалён"
}

cmd_show() {
  awg show || true
  echo
  grep -E '^(Jc|Jmin|Jmax|S1|S2|H[1-4]|ListenPort|MTU)' "$CONF" 2>/dev/null || true
}

case "${1:-}" in
  install) shift; while [[ $# -gt 0 ]]; do case "$1" in
             --port) PORT="${2:?}"; shift 2 ;;
             --subnet) SUBNET="${2:?}"; shift 2 ;;
             --mtu) MTU="${2:?}"; shift 2 ;;
             *) die "неизвестная опция: $1" ;;
           esac; done; cmd_install ;;
  peer)   shift; cmd_peer "$@" ;;
  list)   cmd_list ;;
  rm)     shift; cmd_rm "$@" ;;
  show)   cmd_show ;;
  *) sed -n '2,20p' "$0" ;;
esac
