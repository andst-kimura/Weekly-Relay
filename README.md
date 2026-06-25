# Weekly Relay

> Backlog / Slack / Google Meet 議事録を横断収集し、Gemini AI で要約・分類して Backlog 親課題へ自動転記する週次進捗報告 AI アシスタント

---

## What is this?

週次の進捗報告に関わる以下の手作業を自動化するシステム。

| やること | 手動（今まで） | このシステム |
|---|---|---|
| Backlog 転記 | チケット・Slack を手動確認して書き込み | 毎週金曜 18:00 に自動収集・転記 |
| 議事録反映 | Google Meet 議事録を手動確認 | カレンダー添付 / Meet フォルダから自動取得 |
| ナレッジ蓄積 | 議事録・チケット・Slack が散在 | Firestore `context_snapshots` コレクションへ自動蓄積 |
| 未対応チケット管理 | 手動でBacklogを確認 | N営業日更新なしで Slack DM 通知 |

---

## How it's built

### 技術スタック

| 用途 | 技術 |
|---|---|
| バッチ処理 | Python + `schedule` ライブラリ（ローカル常駐 or タスクスケジューラ） |
| AI エンジン | Gemini API（`gemini-2.5-flash`） |
| Backlog 連携 | Backlog REST API v2 |
| Slack 連携 | Slack SDK（Bot Token） |
| Google 連携 | Google Calendar / Drive / Docs API（OAuth2） |
| データ永続化 | Firestore REST API（gRPC 不使用・`requests` ベース） |
| 設定管理 | `config/config.yaml` + `.env` |

### 設計方針

- **Gemini フォールバック**: `gemini.enabled: false` でルールベース要約に自動フォールバック
- **3段階の親課題判別**: `channel_mapping` 明示指定 → Backlog API で確定的解決 → Gemini 推測
- **矛盾チェック**: 複数親課題への転記内容を Gemini で横断チェック（Gemini 有効時）
- **Firestore REST**: 社内プロキシ環境の gRPC SSL 問題を回避するため、Firestore SDK を使わず REST API を直接呼び出す
- **データキャッシュ**: `--only report` / `--only kb` の2回目以降は `output/cache/YYYYMMDD.pkl` から即座にロードして API 呼び出しを省略
- **議事録要約並列化**: `pre_summarize_meetings()` で全議事録の Gemini 要約を `ThreadPoolExecutor(workers=10)` で並列実行し、以降の処理で `doc["_summary"]` を再利用

---

## Processing Flow

### 週次レポート（毎週金曜 18:00）

```mermaid
sequenceDiagram
    participant SCH as スケジューラ（18:00）
    participant BL as Backlog API
    participant SL as Slack API
    participant CAL as Google Calendar
    participant DRIVE as Google Drive / Docs
    participant GEM as Gemini API
    participant FS as Firestore（REST）

    SCH->>BL: Step 1: 担当・作成・コメントチケット取得
    SCH->>SL: Step 2: 自分の発言・スレッド取得
    SCH->>CAL: Step 3: イベント一覧・工数取得
    SCH->>DRIVE: Step 4: Meet 議事録収集（フォルダ + カレンダー添付）
    SCH->>GEM: Step 5: Gemini クライアント初期化
    SCH->>GEM: Step 6: 議事録要約を並列実行（workers=10）
    SCH->>FS: Step 7: 週次・カレンダーレポートを Firestore へ保存
    SCH->>BL: Step 8: 親課題を判別してコメント転記
    Note over BL,GEM: ① channel_mapping → ② Backlog API 遡及 → ③ Gemini 判別 → ④ 矛盾チェック
    SCH->>FS: Step 9: ナレッジベース生成（tickets / slack / meetings）
```

### 日次アラート / サマリー

| ジョブ | 実行時刻 | 処理内容 |
|---|---|---|
| 未対応チケット警告 | 毎日 09:00（土日祝スキップ） | N 営業日更新なしのチケットを Slack DM 通知 |
| 日次夕方サマリー | 毎日 17:30（土日祝スキップ） | 当日の Backlog・Slack 活動を Gemini で整形して Slack DM 通知 |

---

## Backlog 転記フォーマット

Backlog の各親課題へ投稿されるコメントのフォーマット。Gemini 有効時は AI 整形、無効時はルールベースで生成。

**Gemini 有効時（3セクション構成）**

```
## Weekly Relay 週次進捗レポート YYYY/MM/DD〜YYYY/MM/DD

---

■現在の進捗
・完了した対応内容・合意事項・決定事項
　└ 詳細・補足（サブ項目）
　▼スケジュール（日程が明確な場合のみ）
　　MM/DD〜MM/DD：フェーズ名

■今後のスケジュール
MM/DD：マイルストーン・締切内容
（スケジュールがない場合はセクションごと省略）

■リスク共有
・リスク・懸念事項
（なければ「特になし」）

---
*このコメントは Weekly Relay により自動転記されました*
```

**Gemini 無効時（ルールベースフォールバック）**

```
## Weekly Relay 週次進捗レポート YYYY/MM/DD〜YYYY/MM/DD

---

■現在の進捗
・ISSUE-KEY 課題名（ステータス）
・#channel_name: 発言サンプル
・会議: 議事録タイトル

■リスク共有
特になし

---
*このコメントは Weekly Relay により自動転記されました*
```

**親課題の判別ロジック（4段階）**

| 優先度 | 方法 | 対象 |
|---|---|---|
| ① | `config.yaml` の `channel_mapping` で明示指定 | Slack チャンネル → 親課題 |
| ② | Backlog API で親課題チェーンを遡及（確定的） | Backlog 活動 → SALES_TEAM 親課題 |
| ③ | Gemini AI で判別（Gemini 有効時のみ） | ①②未対応の Slack チャンネル |
| ④ | Gemini で転記内容の矛盾チェック（Gemini 有効時のみ） | 全転記結果を横断検証 |

---

## Google Meet 議事録の収集

2つのソースから議事録を収集し、重複排除してマージします。

| ソース | 対象 | 取得方法 |
|---|---|---|
| Meet Recordings フォルダ | 自分がオーナーの MTG | Drive API でフォルダ内 Docs を取得 |
| カレンダー添付ファイル | 参加した全 MTG | Calendar API のイベント添付から Docs を取得 |

内容が空の議事録（Gemini 未生成）は自動フィルタリングします。

---

## ナレッジベース（Firestore）

KB 生成で作成されるドキュメントは Firestore `context_snapshots` コレクションに保存されます。

| ドキュメントID形式 | 内容 | TTL |
|---|---|---|
| `ticket_{ISSUE-KEY}` | チケット要約・コメント履歴 | 30日 |
| `slack_{YYYYWW}_{channel_name}` | Slack チャンネル週次まとめ | 30日 |
| `meeting_{YYYYMMDD}_{doc_id末尾12文字}` | 議事録全文・AI要約 | 30日 |

**差分スキップ**: チケット KB はすでに保存済みの `backlog_updated_at` と比較し、変更がなければ API 呼び出しをスキップします。

**並列処理の構成**

| 処理 | workers | 備考 |
|---|---|---|
| 議事録要約（Gemini） | 10 | `pre_summarize_meetings()` で事前並列実行 |
| チケット KB | 8 | 各チケット内で `get_issue` + `get_all_comments` をさらに2並列 |
| Slack KB | 3 | Slack API レート制限を考慮して控えめ |
| 議事録 KB | 10 | 事前要約済みのため Gemini 呼び出しなし |

---

## Directory Structure

```
Weekly Relay/
├── main.py                         # エントリーポイント・スケジューラ
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
│   ├── google_docs_client.py       # Google Drive / Docs API クライアント（議事録取得）
│   ├── gemini_client.py            # Gemini API クライアント（要約・判別・転記フォーマット）
│   ├── report_generator.py         # レポート生成（Gemini / ルールベース）・議事録並列要約
│   ├── knowledge_base.py           # ナレッジベース生成（Firestore 保存）
│   ├── firestore_client.py         # Firestore REST API クライアント（gRPC 不使用）
│   ├── ticket_alert.py             # 未対応チケット警告
│   ├── daily_summary.py            # 日次夕方サマリー
│   └── cleanup.py                  # 転記済みコメント・課題の削除ツール
├── tests/                          # pytest テスト
├── docs/
│   └── requirements_v2.md          # 詳細要件定義
└── output/                         # 生成物（Git管理外）
    ├── cache/
    │   └── YYYYMMDD.pkl            # 当日データキャッシュ（--only report/kb で再利用）
    ├── run.log                     # 実行ログ（ローテーション付き）
    └── knowledge/                  # ローカル参照用（Firestore 無効時のフォールバック）
        ├── tickets/
        ├── slack/
        └── meetings/
```

---

## Setup

### 1. 必要な環境

Python 3.11 以上

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
|---|---|
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

必要な OAuth スコープ：

```
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/documents.readonly
```

### 6. Gemini API の設定

1. https://aistudio.google.com/app/apikey で API キーを取得
2. `.env` の `GEMINI_API_KEY` に設定
3. `config/config.yaml` で `gemini.enabled: true` を確認

### 7. Firestore の設定（GCP）

個人用 GCP プロジェクト（`weekly-relay`）に Named Database `weekly-relay` を作成します。DB 名を明示することで、将来 Smart Sync と同じプロジェクトへ統合する際も `smart-sync` DB と明確に区別できます。

> **注意**: 本ツールの Firestore クライアントは gRPC SDK を使用せず、REST API を直接呼び出します。社内プロキシ環境での `SSL_ERROR_SSL` を回避するため、Python 標準の `requests`（urllib3 + certifi）で通信します。

#### 7-1. gcloud CLI のセットアップ（未インストールの場合）

https://cloud.google.com/sdk/docs/install からインストール後：

```bash
gcloud auth login
gcloud config set project weekly-relay
```

#### 7-2. セットアップスクリプトの実行（Git Bash）

```bash
bash infra/setup_firestore.sh
```

スクリプトが以下を自動実行します：

| ステップ | 内容 |
|---|---|
| ADC 認証確認 | 未認証の場合はブラウザで Google ログイン |
| Firestore API 有効化 | `firestore.googleapis.com` |
| DB 作成 | `weekly-relay`（Named Database・`asia-northeast1`） |
| TTL 設定 | `context_snapshots`（30日）/ `sync_logs`（90日） |

#### 7-3. `.env` に GCP 変数を追記

```env
GOOGLE_CLOUD_PROJECT=weekly-relay
FIRESTORE_DATABASE=weekly-relay
```

#### 7-4. ADC 認証（ローカル実行用）

```bash
gcloud auth application-default login
```

### 8. `config/config.yaml` の編集

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

firestore:
  enabled: true                          # false でローカルファイル出力にフォールバック

report:
  output_dir: "output"
  auto_post_to_backlog: true
  dry_run: false                         # true で Backlog 書き込みをスキップ

knowledge_base:
  enabled: true
  output_dir: "output/knowledge"

ticket_alert:
  enabled: true
  stale_business_days: 3                 # N 営業日更新なしで警告
  run_hour: 9
  run_minute: 0

daily_summary:
  enabled: true
  run_hour: 17
  run_minute: 30
```

---

## Usage

### 動作確認（Backlog への書き込みなし）

```bash
python main.py --run-now --dry-run
```

### 今すぐ実行（本番）

```bash
python main.py --run-now
```

### スケジューラ起動（毎週金曜 18:00 に自動実行）

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

### 特定機能のみ実行（`--only` モード）

フル実行の前に特定機能だけを単独で動作確認する際に使用します。Backlog への転記は一切行いません。

```bash
python main.py --only <機能名>
```

| 機能名 | 動作 |
|---|---|
| `backlog` | Backlog 活動の取得件数とチケット一覧（最大10件）をログ出力 |
| `slack` | Slack メッセージの取得件数をログ出力 |
| `calendar` | カレンダーイベント一覧・議事録件数をログ出力 |
| `firestore` | テストドキュメントの書き込み・読み込みで接続確認 |
| `report` | データ収集 → Gemini 要約 → レポート本文生成（転記なし） |
| `kb` | ナレッジベース生成（Firestore `context_snapshots` への保存） |

> **データキャッシュ**: `--only report` / `--only kb` は初回実行時に Backlog・Slack・Calendar・議事録のデータを `output/cache/YYYYMMDD.pkl` に保存します。2回目以降は API 呼び出しをスキップしてキャッシュから即座にロードします（当日中有効）。

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
|---|---|
| `1` | 番号 1 のみ削除 |
| `1,3` | 番号 1 と 3 を削除 |
| `all` | 表示されている全件を削除 |
| `q` | キャンセルして戻る |

> ⚠️ 課題削除は `yes` の入力を要求する二重確認があります。コメント削除は `y` のみです。

---

## Command Reference

| 引数 | 説明 |
|---|---|
| `--run-now` | スケジューラを待たず今すぐ週次レポートを実行 |
| `--dry-run` | Backlog への書き込みをスキップして動作確認 |
| `--only <機能名>` | 特定機能のみ実行（`backlog` / `slack` / `calendar` / `firestore` / `report` / `kb`） |
| `--cleanup` | 転記済みコメント・課題を対話形式で削除 |
| `--run-alert` | 未対応チケット警告を今すぐ実行 |
| `--run-summary` | 日次夕方サマリーを今すぐ実行 |
| `--check-user-id` | Backlog の自分のユーザー ID を確認して終了 |
| `--config PATH` | 設定ファイルのパスを指定（デフォルト: `config/config.yaml`） |

---

## Troubleshooting

| エラー | 原因 | 対処 |
|---|---|---|
| `401 Unauthorized` | Backlog API キーが無効 | キーを再発行して `.env` を更新 |
| `not_in_channel` | Bot が Slack チャンネルに未参加 | `/invite @ボット名` で招待 |
| `403 insufficientPermissions` | Google トークンのスコープ不足 | `google_token.pickle` を削除して再認証 |
| `403 accessNotConfigured` | Drive / Docs API が未有効 | Google Cloud Console で API を有効化 |
| `403 The caller does not have permission` | 議事録ドキュメントの閲覧権限なし | 正常動作（権限なし議事録はスキップ） |
| `403 PERMISSION_DENIED`（Firestore） | ADC 認証が未設定 | `gcloud auth application-default login` を実行 |
| `400 Bad Request`（Firestore） | ドキュメント ID が予約済み形式（`__...__`） | ドキュメント ID を変更（`__` で囲まない） |
| Gemini 判別の誤分類 | 関連性が低い内容が混入 | `channel_mapping` で明示マッピングを追加 |

---

## Testing

```bash
python -m pytest tests/ -v
```

---

## Related

- [docs/requirements_v2.md](docs/requirements_v2.md) — 詳細要件定義（v2.1）
