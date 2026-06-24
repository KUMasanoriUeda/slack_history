import json
import os
import re
import shutil
import logging
import urllib.request
from functools import lru_cache

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FILES_DIR = os.environ.get("FILES_DIR", "/data/files")
os.makedirs(FILES_DIR, exist_ok=True)

MAX_BLOCKS = 48

db.init_db()

app = App(token=os.environ["SLACK_BOT_TOKEN"])
bot_user_id = None


@lru_cache(maxsize=500)
def _resolve_user_id_by_name(name: str) -> str | None:
    cached = db.get_user_by_name(name)
    if cached:
        return cached["user_id"]
    try:
        cursor = None
        while True:
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = app.client.users_list(**kwargs)
            for member in resp["members"]:
                if member.get("deleted"):
                    continue
                profile = member.get("profile", {})
                _cache_user(member)
                for field in (member.get("name", ""), profile.get("display_name", ""), profile.get("real_name", "")):
                    if field and field.lower() == name.lower():
                        return member["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return None
    except Exception:
        return None


@lru_cache(maxsize=500)
def _resolve_user_name(user_id: str) -> str:
    cached = db.get_user_by_id(user_id)
    if cached:
        return cached["display_name"] or cached["real_name"] or user_id
    try:
        resp = app.client.users_info(user=user_id)
        user = resp["user"]
        profile = user.get("profile", {})
        _cache_user(user)
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        return user_id


def _cache_user(member: dict):
    profile = member.get("profile", {})
    db.save_user(
        member["id"],
        member.get("name", ""),
        profile.get("display_name", ""),
        profile.get("real_name", ""),
    )


def _get_bot_user_id():
    global bot_user_id
    if bot_user_id is None:
        auth = app.client.auth_test()
        bot_user_id = auth["user_id"]
    return bot_user_id


def _replace_mentions(text: str) -> str:
    return re.sub(r"<@(\w+)>", lambda m: f"@{_resolve_user_name(m.group(1))}", text)


def _download_file(url: str, dest: str):
    token = os.environ["SLACK_BOT_TOKEN"]
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _user_in_channel(user_id: str, channel_id: str) -> bool:
    try:
        cursor = None
        while True:
            kwargs = {"channel": channel_id, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = app.client.conversations_members(**kwargs)
            if user_id in resp["members"]:
                return True
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return False
    except Exception:
        return False


@app.event("message")
def handle_message(event, say):
    global bot_user_id

    subtype = event.get("subtype")

    if subtype == "message_changed":
        msg = event.get("message", {})
        channel_id = event.get("channel", "")
        db.update_message(channel_id, msg.get("ts", ""), _replace_mentions(msg.get("text", "")))
        logger.info("Updated message: channel=%s ts=%s", channel_id, msg.get("ts"))
        return

    if subtype == "message_deleted":
        channel_id = event.get("channel", "")
        deleted_ts = event.get("deleted_ts", "")
        db.delete_message(channel_id, deleted_ts)
        logger.info("Deleted message: channel=%s ts=%s", channel_id, deleted_ts)
        return

    if subtype == "bot_message":
        return

    channel_id = event.get("channel", "")
    message_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    user_id = event.get("user", "")
    text = event.get("text", "")

    bid = _get_bot_user_id()
    if bid and f"<@{bid}>" in text:
        query = re.sub(rf"<@{bid}>", "", text).strip()
        if not query:
            say(text=_help_text(), thread_ts=message_ts)
            return
        _dispatch(channel_id, query, message_ts, say)
        return

    if user_id and not db.get_user_by_id(user_id):
        _resolve_user_name(user_id)

    text = _replace_mentions(text)
    db.save_message(channel_id, message_ts, thread_ts, user_id, text)
    logger.info("Saved message: channel=%s ts=%s user=%s", channel_id, message_ts, user_id)

    slack_files = event.get("files", [])
    for sf in slack_files:
        file_id = sf.get("id", "")
        file_name = os.path.basename(sf.get("name", "unknown"))
        file_type = sf.get("filetype", "")
        download_url = sf.get("url_private_download") or sf.get("url_private", "")
        if not download_url:
            continue

        safe_name = f"{file_id}_{file_name}"
        local_path = os.path.join(FILES_DIR, safe_name)
        try:
            _download_file(download_url, local_path)
            db.save_file(channel_id, message_ts, thread_ts, user_id, file_id, file_name, file_type, local_path)
            logger.info("Saved file: %s -> %s", file_name, local_path)
        except Exception:
            logger.exception("Failed to download file: %s", file_name)


def _help_text() -> str:
    return (
        "*使い方*\n"
        "*検索（メンション）*\n"
        "• `@bot キーワード` — キーワード検索\n"
        "• `@bot from:@ユーザー キーワード` — ユーザーで絞り込み\n"
        "• `@bot date:2024-01-01 キーワード` — 日付で絞り込み（その日以降）\n"
        "• `@bot date:2024-01-01~2024-01-31 キーワード` — 期間で絞り込み\n"
        "*スラッシュコマンド*\n"
        "• `/hist-search キーワード` — キーワード検索（自分だけに表示）\n"
        "• `/hist-search from:@ユーザー キーワード` — ユーザーで絞り込み\n"
        "• `/hist-search date:2024-01-01 キーワード` — 日付で絞り込み\n"
        "• `/thread 123` — スレッド ID:123 の内容を表示\n"
        "• `/files 123` — スレッド ID:123 の添付ファイルを取得\n"
        "• `/history-help` — この使い方を表示"
    )


def _ephemeral(command, **kwargs):
    kwargs.pop("thread_ts", None)
    app.client.chat_postEphemeral(
        channel=command["channel_id"],
        user=command["user_id"],
        **kwargs,
    )


def _dm(user_id: str, **kwargs):
    resp = app.client.conversations_open(users=[user_id])
    dm_channel = resp["channel"]["id"]
    kwargs.pop("thread_ts", None)
    app.client.chat_postMessage(channel=dm_channel, **kwargs)


@app.command("/history-help")
def cmd_help(ack, command):
    ack()
    _ephemeral(command, text=_help_text())


@app.command("/thread")
def cmd_thread(ack, command):
    ack()
    text = command.get("text", "").strip()
    thread_id = int(text.lstrip("#")) if text.lstrip("#").isdigit() else None
    if thread_id is None:
        _ephemeral(command, text="使い方: `/thread 123`\nスレッド ID を指定してください。")
        return

    thread = db.get_thread_by_id(thread_id)
    if thread and not _user_in_channel(command["user_id"], thread["channel_id"]):
        _ephemeral(command, text="このスレッドのチャンネルへのアクセス権がありません。")
        return

    _handle_thread_detail(thread_id, None, lambda **kwargs: _ephemeral(command, **kwargs), thread=thread)


@app.command("/hist-search")
def cmd_search(ack, command):
    ack()
    query = command.get("text", "").strip()
    logger.info("/hist-search raw text: %r", query)
    if not query:
        _ephemeral(command, text="使い方: `/hist-search キーワード`\n" + _help_text())
        return
    channel_id = command["channel_id"]
    user_id = command["user_id"]
    _handle_search_ephemeral(channel_id, query, user_id, page=1)


@app.command("/files")
def cmd_files(ack, command):
    ack()
    text = command.get("text", "").strip()
    thread_id = int(text.lstrip("#")) if text.lstrip("#").isdigit() else None
    if thread_id is None:
        _ephemeral(command, text="使い方: `/files 123`\nスレッド ID を指定してください。")
        return

    thread = db.get_thread_by_id(thread_id)
    if thread and not _user_in_channel(command["user_id"], thread["channel_id"]):
        _ephemeral(command, text="このスレッドのチャンネルへのアクセス権がありません。")
        return

    _handle_files_dm(thread_id, command["user_id"], thread=thread)


def _dispatch(channel_id: str, query: str, message_ts: str, say):
    _handle_search(channel_id, query, message_ts, say)


def _parse_search_query(query: str) -> dict:
    result = {"keyword": "", "user_id": None, "date_from": None, "date_to": None}

    user_match = re.search(r"from:\s*<@(\w+)(?:\|[^>]*)?>", query)
    if user_match:
        result["user_id"] = user_match.group(1)
        query = query[:user_match.start()] + query[user_match.end():]
    else:
        user_match = re.search(r"from:@(\S+)", query)
        if user_match:
            uid = _resolve_user_id_by_name(user_match.group(1))
            if uid:
                result["user_id"] = uid
            query = query[:user_match.start()] + query[user_match.end():]

    date_match = re.search(r"date:(\d{4}-\d{2}-\d{2})(?:~(\d{4}-\d{2}-\d{2}))?", query)
    if date_match:
        result["date_from"] = date_match.group(1)
        result["date_to"] = date_match.group(2)
        query = query[:date_match.start()] + query[date_match.end():]

    result["keyword"] = query.strip()
    return result


PREVIEW_REPLIES = 3


def _build_search_blocks(channel_id: str, query: str, parsed: dict,
                         threads: list, total: int, page: int, total_pages: int,
                         thread_ts: str, mode: str = "mention") -> tuple[list, str]:
    desc = _build_search_description(parsed)
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{desc}: {total} 件* （ページ {page}/{total_pages}）"},
        },
        {"type": "divider"},
    ]

    for thread in threads:
        msgs = thread["messages"]
        if not msgs:
            continue
        root = msgs[0]
        tid = thread["thread_id"]
        file_count = thread["file_count"]

        header = f"*[#{tid}] {_resolve_user_name(root['user_id'])} ({root['datetime']})*"
        if file_count > 0:
            header += f"  :paperclip: {file_count}件"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": header},
        })

        shown = msgs[:PREVIEW_REPLIES]
        remaining = len(msgs) - len(shown)
        for msg in shown:
            prefix = ":speech_balloon:" if msg["message_ts"] == thread["root_ts"] else "↳"
            text_preview = (msg["text"] or "")[:300]
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{prefix} *{_resolve_user_name(msg['user_id'])}*: {text_preview}"},
            })
        if remaining > 0:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_…他 {remaining} 件の返信（`/thread {tid}` で全文表示）_"}],
            })

        blocks.append({"type": "divider"})

    if total_pages > 1:
        truncated_query = query[:500]
        pagination_value = json.dumps({
            "q": truncated_query, "ch": channel_id, "p": page, "ts": thread_ts,
            "mode": mode,
        }, ensure_ascii=False)

        buttons = []
        if page > 1:
            buttons.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "◀ 前へ"},
                "action_id": "search_prev",
                "value": pagination_value,
            })
        if page < total_pages:
            buttons.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "次へ ▶"},
                "action_id": "search_next",
                "value": pagination_value,
            })
        if buttons:
            blocks.append({"type": "actions", "elements": buttons})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "詳細: `/thread ID` | ファイル: `/files ID`"}],
    })

    return blocks, desc


def _handle_search(channel_id: str, query: str, message_ts: str, say, page: int = 1):
    parsed = _parse_search_query(query)

    if not parsed["keyword"] and not parsed["user_id"] and not parsed["date_from"]:
        say(text="検索条件を指定してください。\n" + _help_text(), thread_ts=message_ts)
        return

    result = db.search_threads(
        channel_id,
        parsed["keyword"],
        user_id=parsed["user_id"],
        date_from=parsed["date_from"],
        date_to=parsed["date_to"],
        per_page=5,
        page=page,
    )

    if not result["threads"]:
        say(text="一致するスレッドは見つかりませんでした。", thread_ts=message_ts)
        return

    blocks, desc = _build_search_blocks(
        channel_id, query, parsed,
        result["threads"], result["total"], page, result["total_pages"],
        message_ts, mode="mention",
    )
    say(blocks=blocks, text=desc, thread_ts=message_ts)


def _handle_search_ephemeral(channel_id: str, query: str, user_id: str, page: int = 1):
    parsed = _parse_search_query(query)

    if not parsed["keyword"] and not parsed["user_id"] and not parsed["date_from"]:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="検索条件を指定してください。\n" + _help_text(),
        )
        return

    result = db.search_threads(
        channel_id,
        parsed["keyword"],
        user_id=parsed["user_id"],
        date_from=parsed["date_from"],
        date_to=parsed["date_to"],
        per_page=5,
        page=page,
    )

    if not result["threads"]:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="一致するスレッドは見つかりませんでした。",
        )
        return

    blocks, desc = _build_search_blocks(
        channel_id, query, parsed,
        result["threads"], result["total"], page, result["total_pages"],
        thread_ts="", mode="ephemeral",
    )
    app.client.chat_postEphemeral(
        channel=channel_id, user=user_id,
        blocks=blocks, text=desc,
    )


@app.action("search_prev")
def handle_search_prev(ack, body):
    ack()
    _handle_pagination(body, delta=-1)


@app.action("search_next")
def handle_search_next(ack, body):
    ack()
    _handle_pagination(body, delta=1)


def _handle_pagination(body, delta: int):
    action = body["actions"][0]
    data = json.loads(action["value"])
    new_page = data["p"] + delta
    channel_id = data["ch"]
    query = data["q"]
    thread_ts = data.get("ts", "")
    mode = data.get("mode", "mention")

    user_id = body["user"]["id"]
    if not _user_in_channel(user_id, channel_id):
        return

    if mode == "ephemeral":
        _handle_search_ephemeral(channel_id, query, user_id, page=new_page)
        return

    parsed = _parse_search_query(query)
    result = db.search_threads(
        channel_id,
        parsed["keyword"],
        user_id=parsed["user_id"],
        date_from=parsed["date_from"],
        date_to=parsed["date_to"],
        per_page=5,
        page=new_page,
    )

    if not result["threads"]:
        return

    blocks, desc = _build_search_blocks(
        channel_id, query, parsed,
        result["threads"], result["total"], new_page, result["total_pages"],
        thread_ts, mode="mention",
    )

    msg_ts = body["message"]["ts"]
    app.client.chat_update(
        channel=body["channel"]["id"],
        ts=msg_ts,
        blocks=blocks,
        text=desc,
    )


def _build_search_description(parsed: dict) -> str:
    parts = []
    if parsed["keyword"]:
        parts.append(f"「{parsed['keyword']}」")
    if parsed["user_id"]:
        parts.append(f"from:{_resolve_user_name(parsed['user_id'])}")
    if parsed["date_from"]:
        if parsed["date_to"] and parsed["date_to"] != parsed["date_from"]:
            parts.append(f"{parsed['date_from']}〜{parsed['date_to']}")
        else:
            parts.append(parsed["date_from"])
    return "検索結果 " + " ".join(parts)


def _say(say, thread_ts: str | None = None, **kwargs):
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    say(**kwargs)


def _handle_thread_detail(thread_id: int, message_ts: str | None, say, thread: dict | None = None):
    if thread is None:
        thread = db.get_thread_by_id(thread_id)
    if not thread:
        _say(say, message_ts, text=f"スレッド #{thread_id} は見つかりませんでした。")
        return

    msgs = thread["messages"]
    files = thread["files"]

    if not msgs:
        _say(say, message_ts, text=f"スレッド #{thread_id} のメッセージは削除されています。")
        return

    root = msgs[0]

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*スレッド #{thread_id}* — {_resolve_user_name(root['user_id'])} ({root['datetime']})"},
        },
        {"type": "divider"},
    ]

    for msg in msgs:
        if len(blocks) >= MAX_BLOCKS - 2:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "（表示上限に達しました）"}],
            })
            break
        prefix = ":speech_balloon:" if msg["message_ts"] == thread["root_ts"] else "↳"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{prefix} *{_resolve_user_name(msg['user_id'])}* _{msg['datetime']}_\n{msg['text'] or ''}"},
        })

    if files:
        blocks.append({"type": "divider"})
        file_lines = "\n".join(f"• {f['file_name']} ({f['file_type']})" for f in files)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*添付ファイル ({len(files)}件)*\n{file_lines}\n\n`/files {thread_id}` でダウンロード"},
        })

    _say(say, message_ts, blocks=blocks, text=f"スレッド #{thread_id}")


def _handle_files_dm(thread_id: int, user_id: str, thread: dict | None = None):
    if thread is None:
        thread = db.get_thread_by_id(thread_id)
    if not thread:
        _dm(user_id, text=f"スレッド #{thread_id} は見つかりませんでした。")
        return

    files = thread["files"]
    if not files:
        _dm(user_id, text=f"スレッド #{thread_id} に添付ファイルはありません。")
        return

    resp = app.client.conversations_open(users=[user_id])
    dm_channel = resp["channel"]["id"]

    app.client.chat_postMessage(
        channel=dm_channel,
        text=f"スレッド #{thread_id} の添付ファイル {len(files)}件 を送信します…",
    )

    files_dir_real = os.path.realpath(FILES_DIR)
    for f in files:
        local_path = f["local_path"]
        if not os.path.realpath(local_path).startswith(files_dir_real + os.sep):
            logger.warning("Path traversal blocked: %s", local_path)
            continue
        if not os.path.exists(local_path):
            app.client.chat_postMessage(
                channel=dm_channel,
                text=f":warning: `{f['file_name']}` のローカルファイルが見つかりません。",
            )
            continue
        try:
            app.client.files_upload_v2(
                channel=dm_channel,
                file=local_path,
                filename=f["file_name"],
                initial_comment=f":paperclip: `{f['file_name']}` (スレッド #{thread_id} より)",
            )
        except Exception:
            logger.exception("Failed to upload file: %s", f["file_name"])
            app.client.chat_postMessage(
                channel=dm_channel,
                text=f":warning: `{f['file_name']}` のアップロードに失敗しました。",
            )


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    bid = _get_bot_user_id()
    logger.info("Bot started as %s", bid)
    handler.start()
