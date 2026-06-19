import sqlite3
import os
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "/data/chat_history.db")
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tokyo"))


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    conn.execute("PRAGMA journal_mode=WAL")

    has_old_fts = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if has_old_fts:
        tokenizer = ""
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()
            tokenizer = row[0] if row else ""
        except Exception:
            pass
        if "trigram" not in tokenizer:
            conn.executescript("""
                DROP TRIGGER IF EXISTS messages_fts_insert;
                DROP TRIGGER IF EXISTS messages_fts_delete;
                DROP TRIGGER IF EXISTS messages_fts_update;
                DROP TABLE IF EXISTS messages_fts;
            """)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            root_ts TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id, root_ts)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            thread_ts TEXT,
            user_id TEXT,
            text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id, message_ts)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread
            ON messages(channel_id, thread_ts);
        CREATE INDEX IF NOT EXISTS idx_messages_user
            ON messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_messages_ts
            ON messages(message_ts);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text,
            content='messages',
            content_rowid=id,
            tokenize='trigram'
        );

        CREATE TRIGGER IF NOT EXISTS messages_fts_insert
            AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_delete
            AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_update
            AFTER UPDATE OF text ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
            INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
        END;

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            thread_ts TEXT,
            user_id TEXT,
            file_id TEXT NOT NULL UNIQUE,
            file_name TEXT,
            file_type TEXT,
            local_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_files_thread
            ON files(channel_id, thread_ts);
    """)

    fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if msg_count > 0 and fts_count == 0:
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()

    conn.close()


def _ensure_thread(conn, channel_id: str, root_ts: str) -> int:
    row = conn.execute(
        "SELECT thread_id FROM threads WHERE channel_id = ? AND root_ts = ?",
        (channel_id, root_ts),
    ).fetchone()
    if row:
        return row["thread_id"]
    cursor = conn.execute(
        "INSERT INTO threads (channel_id, root_ts) VALUES (?, ?)",
        (channel_id, root_ts),
    )
    return cursor.lastrowid


def save_message(channel_id: str, message_ts: str, thread_ts: str | None, user_id: str, text: str) -> int:
    conn = get_connection()
    try:
        root_ts = thread_ts or message_ts
        thread_id = _ensure_thread(conn, channel_id, root_ts)
        conn.execute(
            """INSERT INTO messages (channel_id, message_ts, thread_ts, user_id, text)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(channel_id, message_ts) DO UPDATE SET text=excluded.text, user_id=excluded.user_id""",
            (channel_id, message_ts, thread_ts, user_id, text),
        )
        conn.commit()
        return thread_id
    finally:
        conn.close()


def update_message(channel_id: str, message_ts: str, new_text: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE messages SET text = ? WHERE channel_id = ? AND message_ts = ?",
            (new_text, channel_id, message_ts),
        )
        conn.commit()
    finally:
        conn.close()


def delete_message(channel_id: str, message_ts: str):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM messages WHERE channel_id = ? AND message_ts = ?",
            (channel_id, message_ts),
        )
        conn.commit()
    finally:
        conn.close()


def save_file(channel_id: str, message_ts: str, thread_ts: str | None,
              user_id: str, file_id: str, file_name: str, file_type: str, local_path: str):
    conn = get_connection()
    try:
        root_ts = thread_ts or message_ts
        _ensure_thread(conn, channel_id, root_ts)
        conn.execute(
            """INSERT OR IGNORE INTO files
               (channel_id, message_ts, thread_ts, user_id, file_id, file_name, file_type, local_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, message_ts, thread_ts, user_id, file_id, file_name, file_type, local_path),
        )
        conn.commit()
    finally:
        conn.close()


def _ts_to_datetime(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return ts or ""


def _sanitize_fts_query(keyword: str) -> tuple[str | None, list[str]]:
    words = keyword.split()
    fts_terms = []
    like_terms = []
    for w in words:
        cleaned = w.replace('"', '')
        if not cleaned:
            continue
        if len(cleaned) >= 3:
            fts_terms.append(f'"{cleaned}"')
        else:
            like_terms.append(cleaned)
    fts_query = " AND ".join(fts_terms) if fts_terms else None
    return fts_query, like_terms


def search_threads(channel_id: str, keyword: str, user_id: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   per_page: int = 5, page: int = 1) -> dict:
    empty = {"threads": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}
    conn = get_connection()
    try:
        fts_query = None
        like_terms: list[str] = []
        if keyword:
            fts_query, like_terms = _sanitize_fts_query(keyword)
            if not fts_query and not like_terms:
                return empty

        if fts_query:
            from_clause = "messages m JOIN messages_fts ON messages_fts.rowid = m.id"
            conditions = ["messages_fts MATCH ?"]
            params: list = [fts_query]
        else:
            from_clause = "messages m"
            conditions = []
            params = []

        for term in like_terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("m.text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        conditions.append("m.channel_id = ?")
        params.append(channel_id)

        if user_id:
            conditions.append("m.user_id = ?")
            params.append(user_id)
        if date_from:
            ts_from = str(datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())
            conditions.append("m.message_ts >= ?")
            params.append(ts_from)
        if date_to:
            ts_to = str(datetime.strptime(date_to + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
            conditions.append("m.message_ts <= ?")
            params.append(ts_to)

        where = " AND ".join(conditions)

        total = conn.execute(
            f"""SELECT COUNT(DISTINCT COALESCE(m.thread_ts, m.message_ts))
                FROM {from_clause} WHERE {where}""",
            params,
        ).fetchone()[0]

        if total == 0:
            return empty

        offset = (page - 1) * per_page
        page_params = params + [per_page, offset]

        rows = conn.execute(
            f"""SELECT DISTINCT COALESCE(m.thread_ts, m.message_ts) AS root_ts
                FROM {from_clause}
                WHERE {where}
                ORDER BY root_ts DESC
                LIMIT ? OFFSET ?""",
            page_params,
        ).fetchall()

        threads = []
        for row in rows:
            root_ts = row["root_ts"]
            thread_row = conn.execute(
                "SELECT thread_id FROM threads WHERE channel_id = ? AND root_ts = ?",
                (channel_id, root_ts),
            ).fetchone()
            thread_id = thread_row["thread_id"] if thread_row else "?"

            msgs = conn.execute(
                """SELECT user_id, text, message_ts
                   FROM messages
                   WHERE channel_id = ?
                     AND (message_ts = ? OR thread_ts = ?)
                   ORDER BY message_ts ASC""",
                (channel_id, root_ts, root_ts),
            ).fetchall()

            file_count = conn.execute(
                """SELECT COUNT(*) as cnt FROM files
                   WHERE channel_id = ?
                     AND (message_ts = ? OR thread_ts = ?)""",
                (channel_id, root_ts, root_ts),
            ).fetchone()["cnt"]

            threads.append({
                "thread_id": thread_id,
                "root_ts": root_ts,
                "messages": [
                    {**dict(m), "datetime": _ts_to_datetime(m["message_ts"])}
                    for m in msgs
                ],
                "file_count": file_count,
            })

        total_pages = (total + per_page - 1) // per_page
        return {
            "threads": threads,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    finally:
        conn.close()


def get_thread_by_id(thread_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT channel_id, root_ts FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            return None

        channel_id, root_ts = row["channel_id"], row["root_ts"]
        msgs = conn.execute(
            """SELECT user_id, text, message_ts
               FROM messages
               WHERE channel_id = ?
                 AND (message_ts = ? OR thread_ts = ?)
               ORDER BY message_ts ASC""",
            (channel_id, root_ts, root_ts),
        ).fetchall()

        files = conn.execute(
            """SELECT file_name, file_type, local_path, user_id, message_ts
               FROM files
               WHERE channel_id = ?
                 AND (message_ts = ? OR thread_ts = ?)
               ORDER BY message_ts ASC""",
            (channel_id, root_ts, root_ts),
        ).fetchall()

        return {
            "thread_id": thread_id,
            "channel_id": channel_id,
            "root_ts": root_ts,
            "messages": [
                {**dict(m), "datetime": _ts_to_datetime(m["message_ts"])}
                for m in msgs
            ],
            "files": [dict(f) for f in files],
        }
    finally:
        conn.close()
