#!/bin/sh
set -eu

BACKUP_DIR="/tmp/backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_SRC="/data/chat_history.db"
FILES_SRC="/data/files"

NEXTCLOUD_URL="${NEXTCLOUD_URL%/}"
REMOTE_DIR="${NEXTCLOUD_REMOTE_DIR:-/slack_backup}"

mkdir -p "$BACKUP_DIR"

url_encode_filename() {
    printf '%s' "$1" | sed \
        -e 's/%/%25/g' \
        -e 's/ /%20/g' \
        -e 's/#/%23/g' \
        -e 's/?/%3F/g' \
        -e 's/&/%26/g' \
        -e 's/+/%2B/g' \
        -e 's/\[/%5B/g' \
        -e 's/\]/%5D/g'
}

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

    MANIFEST="/data/uploaded_files.txt"
    touch "$MANIFEST"

    LOCAL_LIST="${BACKUP_DIR}/local_files.txt"
    find "$FILES_SRC" -type f > "$LOCAL_LIST"
    NEW_COUNT=0
    while read -r filepath; do
        filename=$(basename "$filepath")
        if ! grep -qxF "$filename" "$MANIFEST"; then
            encoded=$(url_encode_filename "$filename")
            if curl -s -f --globoff -u "${NEXTCLOUD_USER}:${NEXTCLOUD_PASSWORD}" \
                -T "$filepath" \
                "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}${REMOTE_DIR}/files/${encoded}"; then
                echo "$filename" >> "$MANIFEST"
                NEW_COUNT=$((NEW_COUNT + 1))
            fi
        fi
    done < "$LOCAL_LIST"
    echo "[$(date)] Files uploaded (new: ${NEW_COUNT})"
fi

rm -rf "$BACKUP_DIR"
echo "[$(date)] Backup complete"
