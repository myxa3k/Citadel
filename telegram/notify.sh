#!/bin/bash
# Generic Telegram notifier. Works from ANY host that can reach the internet —
# not tied to this VM. Copy this file plus /etc/telegram-notify.env to any
# other machine (the Proxmox host, another LXC, another VM) and it can push
# into the same chat, tagged with its own hostname.
#
# Usage:
#   notify.sh "message text" [ok|warning|critical] [category]
#
# `category` is optional and only matters if the chat is a Telegram
# supergroup with Topics enabled: it looks up TELEGRAM_TOPIC_<CATEGORY> (e.g.
# category "immich" -> TELEGRAM_TOPIC_IMMICH) in the env file and, if set,
# routes the message into that topic thread. If the category is omitted, the
# variable isn't set, or the chat has no topics at all, the message just goes
# to the chat's main stream — this is the same behavior as before topics
# existed, so nothing breaks for a host that hasn't set any of this up.
#
# Silently no-ops (exit 0) if the credentials file is missing or incomplete,
# so a host that hasn't been set up yet never breaks whatever calls this.
set -euo pipefail

ENVF="/etc/telegram-notify.env"
MSG="${1:-}"
LEVEL="${2:-warning}"
CATEGORY="${3:-}"

[ -n "$MSG" ] || { echo "usage: notify.sh <message> [ok|warning|critical] [category]" >&2; exit 1; }
[ -r "$ENVF" ] || exit 0

set -a; . "$ENVF"; set +a
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || exit 0

case "$LEVEL" in
  critical) ICON="🚨"; TAG="alarm raised" ;;
  ok)       ICON="🛡️"; TAG="all clear"    ;;
  *)        ICON="⚠️"; TAG="anomaly noted" ;;
esac

HOST="$(hostname)"
# The bot's own display name already carries the "Amadeus" persona in
# Telegram's UI — repeating it in the body would be redundant. The body
# instead leads with which host is reporting, since that matters once more
# than one machine pushes into this chat.
TEXT="${ICON} <b>${HOST}</b> — ${TAG}
${MSG}"

ARGS=(
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}"
  --data-urlencode "text=${TEXT}"
  --data-urlencode "parse_mode=HTML"
  --data-urlencode "disable_web_page_preview=true"
)

if [ -n "$CATEGORY" ]; then
  varname="TELEGRAM_TOPIC_$(printf '%s' "$CATEGORY" | tr '[:lower:]' '[:upper:]')"
  topic="${!varname:-}"
  [ -n "$topic" ] && ARGS+=(--data-urlencode "message_thread_id=${topic}")
fi

curl -fsS --max-time 10 \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  "${ARGS[@]}" \
  >/dev/null || echo "notify.sh: failed to reach Telegram" >&2
