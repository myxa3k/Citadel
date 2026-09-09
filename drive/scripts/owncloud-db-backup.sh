#!/bin/bash
# Nightly consistent dump of the ownCloud database.
# Mirrors what Immich does for its own Postgres: a logical dump next to the
# data it belongs to, so any file-level backup picks up a restorable database
# rather than a torn copy of live InnoDB files.
set -euo pipefail

ENV_FILE=/home/shiro/Citadel/drive/owncloud/.env
DEST=/mnt/hdd/owncloud/backups
KEEP=7

DB_ROOT_PASSWORD=$(grep -E '^DB_ROOT_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
[ -n "$DB_ROOT_PASSWORD" ] || { echo "no DB_ROOT_PASSWORD in $ENV_FILE" >&2; exit 1; }

mkdir -p "$DEST"
STAMP=$(date +%Y%m%dT%H%M%S)
OUT="$DEST/owncloud-db-backup-${STAMP}.sql.gz"
TMP="${OUT}.partial"

# --single-transaction gives a consistent snapshot without locking the tables,
# so ownCloud keeps serving while the dump runs.
docker exec -e MYSQL_PWD="$DB_ROOT_PASSWORD" owncloud_mariadb \
  mariadb-dump -u root --single-transaction --quick --routines --events \
  --default-character-set=utf8mb4 owncloud | gzip -c > "$TMP"

# Only publish the file once the whole pipeline succeeded, so a failed run can
# never leave a truncated dump that looks like a valid backup.
mv "$TMP" "$OUT"

ls -1t "$DEST"/owncloud-db-backup-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "ok: $OUT ($(du -h "$OUT" | cut -f1)), keeping last $KEEP"
