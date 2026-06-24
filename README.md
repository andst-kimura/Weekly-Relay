# Weekly Relay

> Backlog / Slack / Google Meet 議事録を横断収集し、Gemini AI で要約・分類して Backlog 親課題へ自動転記する週次進捗報告 AI アシスタント

---

## 概要

Weekly Relay は、毎週の進捗報告に関わる以下の作業を自動化します。

```
[毎週金曜 18:00]

  Backlog       → 自分が担当 / 作成 / コメントした課題
  Slack         → 自分が参加した全チャンネルの発言
  Google Cal    → 参加したミーティング一覧
  Google Meet   → 参加した全 MTG の Gemini 議事録（添付ファイル経由）
        ↓
  Gemini 2.5 Flash による要約・親課題判別
        ↓
  Backlog 親課題へ =Status= / =NextAction= 形式でコメント転記
        ↓
  ナレッジベース（チケット / Slack / 議事録）を Markdown で蓄積
```

---

## 機能一覧

### 1. 週次進捗レポート自動転記

週の活動データを収集し、Backlog の各親課題へコメントを投稿します。

**転記フォーマット**

```
## Weekly Relay 週次進捗レポート YYYY/MM/DD〜YYYY/MM/DD

=Status=
・(MM/DD) 完了した対応内容を箇条書き

=NextAction=
・次のアクション・残課題を箇条書き

---
*このコメントは Weekly Relay により自動転記されました*
```

**親課題の判別ロジック（3段階）**

| 優先度 | 方法 | 対象 |
|--------|------|------|
| ① | `config.yaml` の `channel_mapping` で明示指定 | Slack チャンネル → 親課題 |
| ② | Backlog API で親課題チェーンを遡及（確定的） | Backlog 活動 → SALES_TEAM 親課題 |
| ③ | Gemini AI で判別 | ①②未対応の Slack チャンネル |

### 2. Google Meet 議事録の収集

- **自分がオーナーの MTG**：Meet Recordings フォルダから取得
- **参加した全 MTG**：カレンダーイベントの添付ファイルから取得
- 内容が空の議事録（Gemini 未生成）は自動フィルタリング

### 3. ナレッジベース自動生成

週次レポートと同時に `output/knowledge/` 以下へ Markdown を蓄積します。

| 種別 | 保存先 | 内容 |
|------|--------|------|
| チケット KB | `tickets/ISSUE-KEY.md` | 課題詳細・対応履歴 + Gemini 要約 |
| Slack KB | `slack/YYYYWW_channel.md` | 週次発言・スレッド + Gemini 要約 |
| 議事録 KB | `meetings/YYYYMMDD_タイトル.md` | 議事録全文 + Gemini 要約 |

### 4. 未対応チケット警告（毎朝）

設定した営業日数以上更新のないチケットを検出し、Slack DM で通知します。

### 5. 日次夕方サマリー

当日の Backlog 活動と Slack 発言を Gemini で自然文にまとめ、Slack DM で送信します。

---

## ファイル構成

```
Weekly-Relay/
├── main.py                         # エントリーポイント・スケジューラー
├── requirements.txt                # 依存パッケージ
├── .env                            # APIキー類（要作成・Git管理外）
├── config/
│   ├── config.yaml                 # 全設定（要編集）
│   ├── google_credentials.json     # Google OAuth クライアントキー（要配置）
│   └── google_token.pickle         # 自動生成（初回認証後）
├── src/
│   ├── backlog_client.py           # Backlog REST API v2 クライアント
│   ├── backlog_poster.py           # 親課題判別・コメント転記
│   ├── slack_client.py             # Slack SDK クライアント
│   ├── google_calendar_client.py   # Google Calendar API クライアント
│   ├── google_docs_client.py       # Google Drive / Docs API クライアント
│   ├── gemini_client.py            # Gemini API クライアント（要約・判別）
│   ├── report_generator.py         # レポート生成（Gemini / ルールベース）
│   ├── knowledge_base.py           # ナレッジベース生成
│   ├── ticket_alert.py             # 未対応チケット警告
│   └── daily_summary.py            # 日次夕方サマリー
├── tests/                          # pytest テスト（41件）
├── docs/
│   └── requirements_v2.md          # 詳細要件定義
└── output/                         # 生成物（Git管理外）
    ├── weekly_report_YYYYMMDD.md
    ├── run.log
    └── knowledge/
        ├── tickets/
        ├── slack/
        └── meetings/
```

---

## セットアップ

### 1. 必要な環境

- Python 3.11 以上

```bash
pip install -r requirements.txt
```

### 2. `.env` ファイルの作成

`.env.example` をコピーして `.env` を作成し、各 API キーを設定します。

```bash
cp .env.example .env
```

```env
BACKLOG_API_KEY=xxxxxxxxxxxxxxxxxxxx
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxxxxxx
GEMINI_API_KEY=AIzaSy...
```

### 3. Backlog の設定

**API キー取得**
1. Backlog にログイン
2. **個人設定** → **API** → 「新しいAPIキーを発行する」

**自分のユーザー ID 確認**

```bash
python main.py --check-user-id
```

### 4. Slack App の設定

1. https://api.slack.com/apps で「Create New App」
2. **OAuth & Permissions** → Bot Token Scopes に以下を追加：

| スコープ | 用途 |
|----------|------|
| `channels:history` | パブリックチャンネルのメッセージ取得 |
| `channels:read` | チャンネル一覧の取得 |
| `groups:history` | プライベートチャンネルのメッセージ取得 |
| `groups:read` | プライベートチャンネル一覧 |
| `users:read` | ユーザー情報の取得 |
| `im:write` | DM 送信（日次サマリー用） |
| `chat:write` | メッセージ送信 |

3. ワークスペースにインストールし、Bot Token (`xoxb-...`) を `.env` に設定
4. プライベートチャンネルには `/invite @ボット名` で招待

**自分の Slack User ID 確認**：Slack プロフィール → 「…」→「メンバー ID をコピー」

### 5. Google API の設定

1. https://console.cloud.google.com でプロジェクトを作成
2. 以下の API を有効化：
   - Google Calendar API
   - Google Drive API
   - Google Docs API
3. **認証情報** → **OAuth クライアント ID** → アプリの種類：**デスクトップアプリ**
4. ダウンロードした JSON を `config/google_credentials.json` に保存
5. 初回実行時にブラウザで Google 認証 → `google_token.pickle` が自動生成

### 6. Gemini API の設定

1. https://aistudio.google.com/app/apikey で API キーを取得
2. `.env` の `GEMINI_API_KEY` に設定
3. `config/config.yaml` で `gemini.enabled: true` を確認

### 7. `config/config.yaml` の編集

```yaml
backlog:
  base_url: "https://yourcompany.backlog.jp"
  api_key: "${BACKLOG_API_KEY}"
  my_user_id: 123456                    # --check-user-id で確認
  report_project_key: "SALES_TEAM"      # 転記先プロジェクト

slack:
  bot_token: "${SLACK_BOT_TOKEN}"
  my_user_id: "U01ABCDEFGH"
  channel_mapping:                       # Slack チャンネル → 親課題の明示マッピング
    販売チーム_hblab:
      parent_issue_key: "SALES_TEAM-27"
      label: "販売チーム HBLab"
      project_key: "HBLAB"

google_calendar:
  credentials_file: "config/google_credentials.json"
  calendar_ids:
    - "primary"

google_meet:
  enabled: true
  folder_id: "GoogleDriveのMeet RecordingsフォルダID"

gemini:
  enabled: true
  model: "gemini-2.5-flash"
  api_key: "${GEMINI_API_KEY}"

report:
  output_dir: "output"
  auto_post_to_backlog: true
  dry_run: false                         # true でBacklog書き込みをスキップ

knowledge_base:
  enabled: true
  output_dir: "output/knowledge"

ticket_alert:
  enabled: true
  stale_business_days: 3                 # N営業日更新なしで警告
  run_hour: 9
  run_minute: 0

daily_summary:
  enabled: true
  run_hour: 17
  run_minute: 30
```

---

## 実行方法

### 動作確認（Backlog への書き込みなし）

```bash
python main.py --run-now --dry-run
```

### 今すぐ実行（本番）

```bash
python main.py --run-now
```

### スケジューラー起動（毎週金曜 18:00 に自動実行）

```bash
python main.py
```

### 未対応チケット警告のみ実行

```bash
python main.py --run-alert
```

### 日次サマリーのみ実行

```bash
python main.py --run-summary
```

### 自分の Backlog ユーザー ID 確認

```bash
python main.py --check-user-id
```

### クリーンアップ（転記内容の削除）

動作確認後にテスト転記を削除したい場合に使用します。

```bash
python main.py --cleanup
```

起動すると対話メニューが表示されます。

```
============================================================
  Weekly Relay クリーンアップツール
============================================================

操作を選んでください:
  1. コメントを削除（親課題へのコメント転記分）
  2. 課題を削除（Weekly Relay が起票した課題）
  q. 終了

> 1

プロジェクトの親課題を検索中...

2 件の Weekly Relay コメントが見つかりました:

  [ 1] SALES_TEAM-27 「店舗ACE刷新」  2026/06/24 18:02
       ## Weekly Relay 週次進捗レポート 2026/06/22〜2026/06/24...

  [ 2] SALES_TEAM-254 「6/12発生した障害」  2026/06/24 18:03
       ## Weekly Relay 週次進捗レポート 2026/06/22〜2026/06/24...

削除する番号を入力してください（カンマ区切り / all / q でキャンセル）
> 1,2
削除を実行しますか？（y / n）: y
  ✅ 削除: SALES_TEAM-27 コメント#4521
  ✅ 削除: SALES_TEAM-254 コメント#4522

2 件削除しました。
```

**選択肢の入力形式**

| 入力 | 動作 |
|------|------|
| `1` | 番号 1 のみ削除 |
| `1,3` | 番号 1 と 3 を削除 |
| `all` | 表示されている全件を削除 |
| `q` | キャンセルして戻る |

> ⚠️ 課題削除は `yes` の入力を要求する二重確認があります。コメント削除は `y` のみです。

---

## コマンドライン引数一覧

| 引数 | 説明 |
|------|------|
| `--run-now` | スケジューラーを待たず今すぐ週次レポートを実行 |
| `--dry-run` | Backlog への書き込みをスキップして動作確認 |
| `--cleanup` | 転記済みコメント・課題を対話形式で削除 |
| `--run-alert` | 未対応チケット警告を今すぐ実行 |
| `--run-summary` | 日次夕方サマリーを今すぐ実行 |
| `--check-user-id` | Backlog の自分のユーザー ID を確認して終了 |
| `--config PATH` | 設定ファイルのパスを指定（デフォルト: `config/config.yaml`） |

---

## トラブルシューティング

| エラー | 原因 | 対処 |
|--------|------|------|
| `401 Unauthorized` | Backlog API キーが無効 | キーを再発行して `.env` を更新 |
| `not_in_channel` | Bot が Slack チャンネルに未参加 | `/invite @ボット名` で招待 |
| `403 insufficientPermissions` | Google トークンのスコープ不足 | `google_token.pickle` を削除して再認証 |
| `403 accessNotConfigured` | Drive / Docs API が未有効 | Google Cloud Console でAPIを有効化 |
| `403 The caller does not have permission` | 議事録ドキュメントの閲覧権限なし | 正常動作（権限なし議事録はスキップ） |
| Gemini 判別の誤分類 | 関連性が低い内容が混入 | `channel_mapping` で明示マッピングを追加 |

---

## テスト

```bash
python -m pytest tests/ -v
```

50 件のテストがすべてパスすることを確認しています。
