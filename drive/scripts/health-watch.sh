#!/bin/bash
# Reports disk pressure, unhealthy containers, stale backups and unprotected
# accounts. Runs hourly into the journal and on SSH login via update-motd.d,
# so problems surface without anyone going looking for them.
#
# On top of that it pushes a Telegram message via /usr/local/bin/notify.sh —
# but only on a CHANGE of state (new problem, changed set of problems, or
# recovery), never on every hourly repeat of an unchanged problem. Without
# that de-dup, a single ongoing issue would resend the same message forever.
#
# Findings are grouped into four categories (system, immich, owncloud,
# backups), each tracked and notified independently, so an Immich problem
# doesn't drown out — or get silenced by — an unrelated ownCloud one. If the
# Telegram chat is a supergroup with Topics enabled, notify.sh routes each
# category into its own topic (see docs/notifications.md); if not, they all
# land in the same stream, which is also perfectly fine.
WARN=80
CRIT=90
BACKUP_STALE_HOURS=48
GDRIVE_STALE_HOURS=36
STATE_DIR=/var/lib/health-watch

SYSTEM="" IMMICH="" OWNCLOUD="" BACKUPS=""
add_system()   { SYSTEM="${SYSTEM}${SYSTEM:+$'\n'}$1"; }
add_immich()   { IMMICH="${IMMICH}${IMMICH:+$'\n'}$1"; }
add_owncloud() { OWNCLOUD="${OWNCLOUD}${OWNCLOUD:+$'\n'}$1"; }
add_backups()  { BACKUPS="${BACKUPS}${BACKUPS:+$'\n'}$1"; }

# --- disks: routed by whose data lives on that mount --------------------
for mp in / /mnt/hdd/immich /mnt/hdd/owncloud; do
  case "$mp" in
    /mnt/hdd/immich)   add=add_immich ;;
    /mnt/hdd/owncloud) add=add_owncloud ;;
    *)                 add=add_system ;;
  esac
  if ! mountpoint -q "$mp"; then "$add" "CRITICAL: $mp is NOT MOUNTED"; continue; fi
  read -r use avail <<<"$(df -h --output=pcent,avail "$mp" | tail -1 | tr -d '%')"
  if   [ "$use" -ge "$CRIT" ]; then "$add" "CRITICAL: $mp is ${use}% full (${avail} left)"
  elif [ "$use" -ge "$WARN" ]; then "$add" "WARNING:  $mp is ${use}% full (${avail} left)"
  fi
done

# --- containers: routed by name prefix -----------------------------------
if command -v docker >/dev/null 2>&1; then
  bad=$(docker ps -a --filter 'label=com.docker.compose.project' \
        --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | grep -viE 'Up .*(healthy)|Up [0-9]+ (second|minute|hour|day|week|month)' || true)
  if [ -n "$bad" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      case "$line" in
        immich_*)   add_immich   "WARNING:  container not healthy: $line" ;;
        owncloud_*) add_owncloud "WARNING:  container not healthy: $line" ;;
        *)          add_system   "WARNING:  container not healthy: $line" ;;
      esac
    done <<< "$bad"
  fi
fi

# --- backups: a job that quietly stopped working is worse than no backup,
# because it buys false confidence. A backup job can run nightly for weeks
# and produce nothing restorable without anyone noticing. -----------------
for conf in /etc/restic/*.conf; do
  [ -e "$conf" ] || continue
  p=$(basename "$conf" .conf)
  stamp="/var/lib/restic/${p}.last-success"
  if [ ! -f "$stamp" ]; then
    add_backups "WARNING:  backup '$p' has never completed successfully"; continue
  fi
  age=$(( ( $(date +%s) - $(date -d "$(cat "$stamp")" +%s) ) / 3600 ))
  [ "$age" -ge "$BACKUP_STALE_HOURS" ] && add_backups "CRITICAL: backup '$p' last succeeded ${age}h ago"
done

# Same staleness check for the Google Drive pull — only while its timer is
# actually enabled, so this stays silent while the sync is deliberately off
# rather than nagging about a job nobody wants running. The moment it's
# re-enabled, this check is already armed: this exact silent-for-days failure
# is what let three nights of "invalid_grant" errors go unnoticed in 2026-09.
if systemctl is-enabled --quiet gdrive-sync.timer 2>/dev/null; then
  stamp=/var/lib/rclone/gdrive.last-success
  if [ ! -f "$stamp" ]; then
    add_backups "WARNING:  gdrive-sync has never completed successfully"
  else
    age=$(( ( $(date +%s) - $(date -d "$(cat "$stamp")" +%s) ) / 3600 ))
    [ "$age" -ge "$GDRIVE_STALE_HOURS" ] && add_backups "CRITICAL: gdrive-sync last succeeded ${age}h ago"
  fi
fi

# Catch an account whose data nobody is backing up — e.g. a new user was
# created and never assigned to a restic profile.
if [ -d /mnt/hdd/immich/upload ] && [ -r /etc/restic ]; then
  known=$(cat /etc/restic/*.conf 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | tr '\n' ' ')
  for d in /mnt/hdd/immich/upload/*/; do
    [ -d "$d" ] || continue
    uid=$(basename "$d")
    case " $known " in
      *" $uid "*) ;;
      *) add_backups "WARNING:  Immich user $uid is in NO backup profile ($(du -sh "$d" 2>/dev/null | cut -f1))" ;;
    esac
  done
fi

[ -f /var/run/reboot-required ] && add_system "NOTICE:   reboot required (kernel or libc updated)"

# --- combined output for the journal / MOTD, unchanged from before ------
all="$(printf '%s\n%s\n%s\n%s' "$SYSTEM" "$IMMICH" "$OWNCLOUD" "$BACKUPS" | sed '/^$/d')"
if [ -n "$all" ]; then
  echo "$all"
elif [ "${1:-}" = "--verbose" ]; then
  echo "all good: disks ok, containers healthy, backups fresh"
fi

# --verbose is for a human running this by hand — never push Telegram, and
# never ping the dead-man's-switch below, from that: only from the
# unattended hourly/login-triggered run.
[ "${1:-}" = "--verbose" ] && exit 0

# External dead-man's-switch (healthchecks.io or similar): pinged once per
# unattended run, unconditionally, regardless of what was found above. This
# isn't about whether anything's wrong — Telegram already covers that — it's
# proof this script itself executed to completion. If health-watch.timer
# stops firing, the script crashes, or the VM loses network entirely, the
# ping stops arriving and the external service raises the alarm on its own —
# it doesn't depend on this machine, or its own alerting, being alive to say
# so. See /etc/healthchecks-ping.env.example for how to wire one up; silently
# does nothing if that file doesn't exist.
if [ -r /etc/healthchecks-ping.env ]; then
  set -a; . /etc/healthchecks-ping.env; set +a
  if [ -n "${HEALTHCHECKS_PING_URL:-}" ]; then
    curl -fsS --max-time 10 -o /dev/null "$HEALTHCHECKS_PING_URL" || true
  fi
fi

install -d -m 700 "$STATE_DIR" 2>/dev/null || true

# The worst label already present in this category's own findings decides
# the Telegram icon — CRITICAL beats WARNING beats NOTICE — so the icon
# reflects what actually happened instead of always shouting "critical"
# regardless of severity.
severity_of() {
  case "$1" in
    *CRITICAL:*) echo critical ;;
    *)           echo warning ;;
  esac
}

# One independent notify+state cycle per category, so an Immich problem and
# an ownCloud problem are tracked (and recovered) separately, and each can be
# routed to its own Telegram topic via notify.sh's third argument.
notify_category() {
  local name="$1" text="$2" state="$STATE_DIR/last-$1"
  local prev="" curr=""
  [ -f "$state" ] && prev="$(cat "$state")"
  if [ -n "$text" ]; then
    curr="$(printf '%s' "$text" | sha256sum | cut -d' ' -f1)"
    [ "$curr" != "$prev" ] && /usr/local/bin/notify.sh "$text" "$(severity_of "$text")" "$name"
    printf '%s' "$curr" > "$state"
  elif [ -f "$state" ]; then
    /usr/local/bin/notify.sh "All systems nominal." ok "$name"
    rm -f "$state"
  fi
}

notify_category system   "$SYSTEM"
notify_category immich   "$IMMICH"
notify_category owncloud "$OWNCLOUD"
notify_category backups  "$BACKUPS"

exit 0
