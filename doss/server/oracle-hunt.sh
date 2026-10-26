#!/usr/bin/env bash
#
# oracle-hunt.sh — «охота» за бесплатной ARM-ёмкостью в Oracle Cloud Always Free.
#
# Ошибка "Out of host capacity" на VM.Standard.A1.Flex — норма: бесплатные
# ARM-ядра в популярных регионах разобраны. Инстанс создаётся перебором
# availability domains и повторными попытками (обычно 20 минут – 3 суток).
#
# Требуется настроенный OCI CLI (`oci setup config`) и ключ ssh.
#
# Пример:
#   ./oracle-hunt.sh \
#     --compartment ocid1.tenancy.oc1..aaaa... \
#     --subnet ocid1.subnet.oc1.... \
#     --ssh-key ~/.ssh/id_ed25519.pub \
#     --ocpus 2 --memory 12 \
#     --telegram-token 123:ABC --telegram-chat 12345678
#
set -uo pipefail

COMPARTMENT=""; SUBNET=""; SSH_KEY=""; IMAGE=""; SHAPE="VM.Standard.A1.Flex"
OCPUS="2"; MEMORY="12"; NAME="doss-vpn"
SLEEP_SECONDS=45; MAX_TRIES=0   # 0 = бесконечно
TG_TOKEN=""; TG_CHAT=""

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '\033[1;31mошибка:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compartment) COMPARTMENT="${2:?}"; shift 2 ;;
    --subnet)      SUBNET="${2:?}"; shift 2 ;;
    --ssh-key)     SSH_KEY="${2:?}"; shift 2 ;;
    --image)       IMAGE="${2:?}"; shift 2 ;;
    --shape)       SHAPE="${2:?}"; shift 2 ;;
    --ocpus)       OCPUS="${2:?}"; shift 2 ;;
    --memory)      MEMORY="${2:?}"; shift 2 ;;
    --name)        NAME="${2:?}"; shift 2 ;;
    --sleep)       SLEEP_SECONDS="${2:?}"; shift 2 ;;
    --max-tries)   MAX_TRIES="${2:?}"; shift 2 ;;
    --telegram-token) TG_TOKEN="${2:?}"; shift 2 ;;
    --telegram-chat)  TG_CHAT="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "неизвестная опция: $1" ;;
  esac
done

command -v oci >/dev/null || die "нет OCI CLI. Установка: https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm"
[[ -n "$COMPARTMENT" ]] || die "--compartment обязателен"
[[ -n "$SUBNET" ]] || die "--subnet обязателен"
[[ -n "$SSH_KEY" && -f "$SSH_KEY" ]] || die "--ssh-key: файл не найден"

notify() {
  [[ -n "$TG_TOKEN" && -n "$TG_CHAT" ]] || return 0
  curl -fsS --max-time 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" -d "text=$1" >/dev/null 2>&1 || true
}

# --- образ: если не задан, берём свежий Ubuntu 24.04 для ARM
if [[ -z "$IMAGE" ]]; then
  log "ищу образ Ubuntu 24.04 (aarch64)"
  IMAGE="$(oci compute image list \
      --compartment-id "$COMPARTMENT" \
      --operating-system "Canonical Ubuntu" \
      --operating-system-version "24.04" \
      --shape "$SHAPE" \
      --sort-by TIMECREATED --sort-order DESC \
      --query 'data[0].id' --raw-output 2>/dev/null)"
  [[ -n "$IMAGE" && "$IMAGE" != "null" ]] || die "не нашёл образ, укажи --image вручную"
  log "образ: $IMAGE"
fi

log "ищу availability domains"
mapfile -t ADS < <(oci iam availability-domain list \
  --compartment-id "$COMPARTMENT" --query 'data[].name' --raw-output 2>/dev/null)
[[ ${#ADS[@]} -gt 0 ]] || die "не удалось получить availability domains"
log "AD: ${ADS[*]}"

# --- проверяем, нет ли уже созданного инстанса с таким именем
EXISTING="$(oci compute instance list --compartment-id "$COMPARTMENT" \
  --display-name "$NAME" --lifecycle-state RUNNING \
  --query 'data[0].id' --raw-output 2>/dev/null)"
if [[ -n "$EXISTING" && "$EXISTING" != "null" ]]; then
  log "инстанс '$NAME' уже существует: $EXISTING — выхожу"
  exit 0
fi

try=0
while :; do
  try=$((try+1))
  for AD in "${ADS[@]}"; do
    log "попытка #$try, AD $AD"
    OUT="$(oci compute instance launch \
      --compartment-id "$COMPARTMENT" \
      --availability-domain "$AD" \
      --shape "$SHAPE" \
      --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY}" \
      --image-id "$IMAGE" \
      --subnet-id "$SUBNET" \
      --display-name "$NAME" \
      --assign-public-ip true \
      --metadata "{\"ssh_authorized_keys\": \"$(cat "$SSH_KEY")\"}" \
      --wait-for-state RUNNING \
      --wait-interval-seconds 10 2>&1)"

    if printf '%s' "$OUT" | grep -q '"lifecycle-state": "RUNNING"'; then
      IP="$(printf '%s' "$OUT" | jq -r '.data."public-ip" // empty' 2>/dev/null)"
      if [[ -z "$IP" ]]; then
        ID="$(printf '%s' "$OUT" | jq -r '.data.id')"
        VNIC="$(oci compute instance list-vnics --instance-id "$ID" --query 'data[0]."public-ip"' --raw-output 2>/dev/null)"
        IP="$VNIC"
      fi
      log "ГОТОВО! инстанс запущен, IP: ${IP:-смотри консоль}"
      notify "Doss: бесплатный сервер создан, IP ${IP:-?}"
      printf '%s\n' "$IP" > "$HOME/.doss-server-ip"
      exit 0
    fi

    if printf '%s' "$OUT" | grep -qi 'out of host capacity\|OutOfCapacity\|capacity'; then
      log "нет ёмкости в этом AD — иду дальше"
    elif printf '%s' "$OUT" | grep -qi 'LimitExceeded\|QuotaExceeded'; then
      log "упёрся в квоту: $(printf '%s' "$OUT" | jq -r '.message // empty' 2>/dev/null)"
      die "проверь лимиты: 2 OCPU / 12 GB на Always Free (урезано в июне 2026)"
    else
      log "ответ Oracle: $(printf '%s' "$OUT" | tr '\n' ' ' | cut -c1-300)"
    fi
  done

  if [[ "$MAX_TRIES" -gt 0 && "$try" -ge "$MAX_TRIES" ]]; then
    die "исчерпал $MAX_TRIES попыток. Запусти позже/ночью — ёмкость освобождается волнами"
  fi
  log "пауза ${SLEEP_SECONDS}s, затем снова (попытка $((try+1)))"
  sleep "$SLEEP_SECONDS"
done
