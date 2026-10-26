#!/usr/bin/env bash
#
# bootstrap-remote.sh — «одна команда» на свежем сервере: скачивает Doss и
# запускает установку целиком. Токен бота можно передать сразу или позже.
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/<branch>/doss/deploy/bootstrap-remote.sh | sudo bash
#   curl -fsSL ... | sudo bash -s -- --token 123:ABC
#
# Если репозиторий приватный, git clone не пройдёт — скопируй папку doss/
# на сервер (scp -r doss root@server:/opt/doss) и запусти deploy/install-all.sh.
#
set -euo pipefail

REPO="${DOSS_REPO:-https://github.com/pakipsaki333-cpu/vlad-neon-portfolio.git}"
BRANCH="${DOSS_BRANCH:-main}"
DIR="${DOSS_SRC:-/opt/doss-src}"
ARGS=("$@")

log() { printf '\033[1;36m[doss]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[doss]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "запускай от root: … | sudo bash"

command -v git >/dev/null || {
  log "ставлю git"
  apt-get update -qq && apt-get install -y -qq git ca-certificates
}

if [[ -d "$DIR/.git" ]]; then
  log "обновляю $DIR (ветка $BRANCH)"
  git -C "$DIR" fetch --depth 1 origin "$BRANCH" \
    && git -C "$DIR" checkout -f FETCH_HEAD
else
  log "клонирую $REPO → $DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR" || die \
"не смог склонировать репозиторий. Если он приватный — скопируй папку doss/ на сервер и запусти /opt/doss/deploy/install-all.sh"
fi

log "запускаю установку"
exec bash "$DIR/doss/deploy/install-all.sh" "${ARGS[@]}"
