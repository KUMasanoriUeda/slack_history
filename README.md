# Slack Chat History Bot

Slack の会話履歴をリアルタイムで保存し、キーワード・ユーザー・日付で検索できる Bot です。Slack の保存期限（無料プランは90日）が切れた後でも、保存済みのメッセージや添付ファイルにアクセスできます。

## 機能

- **リアルタイム保存** — Bot が参加しているチャンネルの全メッセージ・スレッド返信・添付ファイルを自動保存
- **メッセージ編集/削除の追従** — 編集・削除もリアルタイムで DB に反映
- **キーワード検索** — メンションで全文検索（日本語対応、FTS5 trigram）
- **絞り込み** — ユーザー・日付・期間で検索結果をフィルタリング
- **スレッド ID** — 検索結果の各スレッドに ID が付与され、後から簡単にアクセス可能
- **添付ファイル取得** — 保存済みのファイルを DM で再送信
- **ページネーション** — 検索結果をページ切り替え（メッセージをその場で更新）
- **エフェメラル応答** — スラッシュコマンドの結果は実行者だけに表示

## コマンド一覧

### 検索（メンション）

| コマンド | 説明 |
|---|---|
| `@bot キーワード` | キーワード検索 |
| `@bot from:@ユーザー キーワード` | ユーザーで絞り込み |
| `@bot date:2024-01-01 キーワード` | 指定日以降で絞り込み |
| `@bot date:2024-01-01~2024-03-31 キーワード` | 期間指定 |

### スラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/thread 123` | スレッド #123 の全文をエフェメラル表示 |
| `/files 123` | スレッド #123 の添付ファイルを DM で送信 |
| `/history-help` | 使い方を表示 |

## セットアップ

### 1. Slack App の作成

[api.slack.com/apps](https://api.slack.com/apps) で新規アプリを作成し、以下を設定します。

**Socket Mode**

- Settings > Socket Mode を有効化
- App-Level Token を生成（`connections:write` スコープ）→ `xapp-...`

**Event Subscriptions**

- 有効化し、Bot Events に以下を追加:
  - `message.channels`
  - `message.groups`（プライベートチャンネルが必要な場合）

**OAuth & Permissions — Bot Token Scopes**

| スコープ | 用途 |
|---|---|
| `channels:history` | パブリックチャンネルのメッセージ受信 |
| `groups:history` | プライベートチャンネル（必要なら） |
| `chat:write` | 検索結果の返信 |
| `files:read` | 添付ファイルのダウンロード |
| `files:write` | ファイルの再アップロード |
| `users:read` | ディスプレイ名の取得 |
| `im:write` | DM でのファイル送信 |

**Slash Commands**

| コマンド | Description |
|---|---|
| `/thread` | スレッドの詳細を表示 |
| `/files` | スレッドの添付ファイルを取得 |
| `/history-help` | 使い方を表示 |

**Interactivity**

- Settings > Interactivity & Shortcuts を有効化（Socket Mode のため URL 不要）

**インストール**

- ワークスペースにインストールし、Bot User OAuth Token (`xoxb-...`) を取得

### 2. 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集してトークンを記入:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
```

### 3. 起動

```bash
docker compose up -d --build
```

### 4. Bot をチャンネルに招待

対象チャンネルで `/invite @Bot名` を実行すると、メッセージの保存が開始されます。
複数チャンネルに招待可能で、チャンネルごとにメッセージが保存・検索されます。

### 5. Nextcloud バックアップ（オプション）

`.env` に Nextcloud の情報を追加すると、6時間ごとに DB と添付ファイルが自動アップロードされます。

```
NEXTCLOUD_URL=https://your-nextcloud.example.com
NEXTCLOUD_USER=your-username
NEXTCLOUD_PASSWORD=your-password
NEXTCLOUD_REMOTE_DIR=/slack_backup
```

バックアップ先の Nextcloud フォルダは自動作成されます。

## 技術構成

| コンポーネント | 技術 |
|---|---|
| 言語 | Python 3.12 |
| Slack SDK | slack-bolt (Socket Mode) |
| データベース | SQLite (WAL mode, FTS5 trigram) |
| コンテナ | Docker (非root ユーザー実行) |

## ファイル構成

```
.
├── app.py              # Bot 本体（イベント処理・コマンド・検索）
├── db.py               # SQLite 操作（FTS5 全文検索・CRUD）
├── Dockerfile
├── Dockerfile.backup    # Nextcloud バックアップ用コンテナ
├── backup.sh            # バックアップスクリプト
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

## データの保存先

Docker volume `bot-data` に以下が保存されます:

- `/data/chat_history.db` — メッセージ・スレッド・ファイルメタデータ
- `/data/files/` — ダウンロードされた添付ファイル

## License

MIT
