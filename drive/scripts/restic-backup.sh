#!/bin/bash
# Back up one person's data to their own restic repository.
#
#   /usr/local/bin/restic-backup.sh <profile>
#
# A profile is two root-only files in /etc/restic:
#   <profile>.env   repository URL + storage credentials + encryption password
#   <profile>.conf  which Immich user IDs and which ownCloud users belong here
#
# Each person gets their own profile, their own bucket and their own encryption
# password, so one person's backup is unreadable to the other and each pays for
# only their own data.
set -euo pipefail

PROFILE="${1:?usage: restic-backup.sh <profile>}"
ENVF="/etc/restic/${PROFILE}.env"
CONFF="/etc/restic/${PROFILE}.conf"
STAMP_DIR=/var/lib/restic

for f in "$ENVF" "$CONFF"; do
  [ -r "$f" ] || { echo "missing or unreadable: $f" >&2; exit 1; }
done

set -a; . "$ENVF"; . "$CONFF"; set +a
mkdir -p "$STAMP_DIR"

PATHS=()
add() {
  if [ -e "$1" ]; then PATHS+=("$1"); else echo "  note: not present, skipping — $1"; fi
}

echo "== collecting paths for profile '$PROFILE'"
for uid in ${IMMICH_UIDS:-}; do
  add "/mnt/hdd/immich/upload/$uid"    # originals — the irreplaceable part
  add "/mnt/hdd/immich/profile/$uid"   # avatar, a few KB
done
add "/mnt/hdd/immich/backups"          # nightly Postgres dumps: albums, faces, dates

for u in ${OC_USERS:-}; do
  # Only files/. Siblings cache/, uploads/ and files_trashbin/ are transient
  # or already-deleted data and are deliberately left out.
  add "/mnt/hdd/owncloud/files/$u/files"
done
add "/mnt/hdd/owncloud/backups"        # nightly MariaDB dumps: shares, users

add "/home/shiro/Citadel/drive"                # compose files + .env — rebuilds the whole stack

# Derived data is never backed up: Immich regenerates thumbs/ and
# encoded-video/ from the originals, so paying to store them is pure waste.

[ ${#PATHS[@]} -gt 0 ] || { echo "nothing to back up" >&2; exit 1; }
printf '  include: %s\n' "${PATHS[@]}"

# The live database directories sit under ~/Citadel/drive. Copying them while
# Postgres and MariaDB are running yields a torn, unrestorable snapshot — and
# we already take proper logical dumps at 02:00 and 02:30, which ARE
# restorable. Keeping the torn copy too would only invite someone to restore
# the wrong thing.
EXCLUDES=(
  --exclude /home/shiro/Citadel/drive/immich/postgres
  --exclude /home/shiro/Citadel/drive/owncloud/mysql
  --exclude /home/shiro/Citadel/drive/owncloud/redis
  --exclude /mnt/hdd/immich/lost+found
  --exclude /mnt/hdd/owncloud/lost+found
)

echo "== backup"
restic backup --tag "$PROFILE" --exclude-caches --verbose "${EXCLUDES[@]}" "${PATHS[@]}"

echo "== retention"
restic forget --tag "$PROFILE" \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 \
  --prune

date -Is > "${STAMP_DIR}/${PROFILE}.last-success"
echo "== done: $(cat "${STAMP_DIR}/${PROFILE}.last-success")"
