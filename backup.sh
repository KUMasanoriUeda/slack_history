#!/bin/sh
set -eu

BACKUP_DIR="/tmp/backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_SRC="/data/chat_history.db"
FILES_SRC="/data/files"

NEXTCLOUD_URL="${NEXTCLOUD_URL%/}"
REMOTE_DIR="${NEXTCLOUD_REMOTE_DIR:-/slack_backup}"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup..."

# SQLite のオンラインバックアップ（WALモードでも安全）
if [ -f "$DB_SRC" ]; then
    sqlite3 "$DB_SRC" ".backup '$BACKUP_DIR/chat_history.db'"
    echo "[$(date)] DB backup created"
fi

# DB をアップロード
curl -s -f -u "${NEXTCLOUD_USER}:${NEXTCLOUD_PASSWORD}" \
    -X MKCOL "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}${REMOTE_DIR}" 2>/dev/null || true

curl -s -f -u "${NEXTCLOUD_USER}:${NEXTCLOUD_PASSWORD}" \
    -T "$BACKUP_DIR/chat_history.db" \
    "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}${REMOTE_DIR}/chat_history.db"
echo "[$(date)] DB uploaded"

# ファイルをアップロード
if [ -d "$FILES_SRC" ]; then
    curl -s -f -u "${NEXTCLOUD_USER}:${NEXTCLOUD_PASSWORD}" \
        -X MKCOL "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}${REMOTE_DIR}/files" 2>/dev/null || true

    find "$FILES_SRC" -type f | while read -r filepath; do
        filename=$(basename "$filepath")
        curl -s -f -u "${NEXTCLOUD_USER}:${NEXTCLOUD_PASSWORD}" \
            -T "$filepath" \
            "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}${REMOTE_DIR}/files/${filename}"
    done
    echo "[$(date)] Files uploaded"
fi

rm -rf "$BACKUP_DIR"
echo "[$(date)] Backup complete"
