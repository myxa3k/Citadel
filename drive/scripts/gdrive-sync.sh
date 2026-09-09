#!/bin/bash
# Pull Google Drive into the ownCloud data directory, then make ownCloud notice.
#
# `copy`, never `sync`: this mirrors Drive INTO the server but never deletes
# anything locally. Cleaning up Google Drive later must not wipe the copy that
# replaced it.
set -uo pipefail

REMOTE="gdrive:"
OC_USER="myxa3k"
DEST="/mnt/hdd/owncloud/files/${OC_USER}/files/GoogleDrive"
LOG=/var/log/gdrive-sync.log
STAMP=/var/lib/rclone/gdrive.last-success

mkdir -p "$DEST" /var/lib/rclone
exec >>"$LOG" 2>&1
echo "=== $(date -Is) start ==="

rclone copy "$REMOTE" "$DEST" \
  --config /root/.config/rclone/rclone.conf \
  --drive-export-formats docx,xlsx,pptx,svg \
  --drive-skip-dangling-shortcuts \
  --drive-acknowledge-abuse \
  --transfers 8 --checkers 16 --tpslimit 10 \
  --fast-list \
  --retries 3 --low-level-retries 10 \
  --stats 5m --stats-one-line \
  --log-level INFO
RC=$?

if [ $RC -ne 0 ]; then
  echo "=== $(date -Is) rclone exited $RC — not scanning ==="
  exit $RC
fi

# ownCloud only knows about files it put there itself. Anything written to the
# data directory from outside stays invisible until the filecache is refreshed.
# www-data (uid 33) must own it or ownCloud cannot read it.
chown -R 33:0 "$DEST"
docker exec -u www-data owncloud_server \
  occ files:scan --path="/${OC_USER}/files/GoogleDrive" --quiet

date -Is > "$STAMP"
echo "=== $(date -Is) done ==="
