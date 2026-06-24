# 週次進捗報告 自動化ツール セットアップガイド

## 概要

Backlog・Slack・Googleカレンダーから自分の活動を毎週金曜18時に自動収集し、
Backlogの進捗報告課題へ転記するツールです。

```
[毎週金曜 18:00]
      ↓
  Backlog API  →  自分が担当/作成/コメントした課題
  Slack API    →  自分が参加する全チャンネルの発言
  Google Cal   →  スケジュール・工数情報
      ↓
  レポート生成（Claude API or ルールベース）
      ↓
  Backlog SALES_TEAM プロジェクトへ転記
```

---

## 1. 必要な環境

- Python 3.11 以上
- pip

```bash
pip install -r requirements.txt
```

---

## 2. 各サービスの事前準備

### 2-1. Backlog APIキー取得

1. Backlogにログイン
2. **個人設定** > **API** > 「新しいAPIキーを発行する」
3. 発行されたキーを `config/config.yaml` の `backlog.api_key` に設定

### 2-2. 自分のBacklogユーザーIDを確認

APIキー設定後、以下のコマンドで自分のIDを確認できます：

```bash
python main.py --check-user-id
```

表示されたIDを `config/config.yaml` の `backlog.my_user_id` に設定してください。

---

### 2-3. Slack App の作成とトークン取得

1. https://api.slack.com/apps にアクセス
2. **「Create New App」** > **「From scratch」** を選択
3. App Name: 任意（例: `Weekly Report Bot`）、Workspace: 自分のワークスペース
4. 左メニュー **「OAuth & Permissions」** を開く
5. **「Bot Token Scopes」** に以下のスコープを追加：

   | スコープ | 用途 |
   |---|---|
   | `channels:history` | パブリックチャンネルのメッセージ取得 |
   | `channels:read` | チャンネル一覧の取得 |
   | `groups:history` | プライベートチャンネルのメッセージ取得 |
   | `groups:read` | プライベートチャンネル一覧 |
   | `users:read` | ユーザー情報の取得 |

6. **「Install to Workspace」** でインストール
7. 生成された **「Bot User OAuth Token」** (`xoxb-...`) を `config/config.yaml` の `slack.bot_token` に設定

### 2-4. 自分のSlack User IDを確認

1. Slackアプリを開く
2. 自分のプロフィールを開く
3. **「…」（その他）** > **「メンバーIDをコピー」** を選択
4. `UXXXXXXXXX` の形式のIDを `config/config.yaml` の `slack.my_user_id` に設定

> ⚠️ **Botをチャンネルに招待する必要があります**  
> プライベートチャンネルは `/invite @Weekly Report Bot` で招待してください。

---

### 2-5. Google Calendar 認証設定

1. https://console.cloud.google.com にアクセス
2. 新規プロジェクト作成（または既存プロジェクトを選択）
3. **「APIとサービス」** > **「ライブラリ」** > 「Google Calendar API」を有効化
4. **「APIとサービス」** > **「認証情報」** > **「認証情報を作成」** > **「OAuthクライアントID」**
5. アプリケーションの種類: **「デスクトップアプリ」** を選択
6. ダウンロードしたJSONファイルを `config/google_credentials.json` として保存

> 初回実行時にブラウザが開いてGoogleログイン画面が表示されます。  
> 認証後、`config/google_token.pickle` が自動生成され、次回以降は自動認証されます。

---

### 2-6. Claude API（任意・AI要約機能）

> **組織管理者から権限が付与されている場合のみ設定してください。**  
> 未設定の場合はルールベースの要約が自動的に使われます。

1. https://console.anthropic.com にアクセス
2. **「API Keys」** から新しいキーを発行
3. `config/config.yaml` に設定：

```yaml
claude:
  api_key: "sk-ant-api03-..."
  enabled: true
```

---

## 3. 設定ファイルの記載例

```yaml
backlog:
  base_url: "https://adastria.backlog.jp"
  api_key: "xxxxxxxxxxxxxxxxxxxx"
  my_user_id: 123456
  report_project_key: "SALES_TEAM"

slack:
  bot_token: "xoxb-xxxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx"
  my_user_id: "U01ABCDEFGH"

google_calendar:
  credentials_file: "config/google_credentials.json"
  calendar_ids:
    - "primary"

claude:
  api_key: ""          # 未設定でもOK（ルールベース要約を使用）
  enabled: false

report:
  output_dir: "output"
  auto_post_to_backlog: true
  dry_run: false       # trueにするとBacklogへの書き込みをスキップ

schedule:
  day_of_week: "friday"
  hour: 18
  minute: 0
```

---

## 4. 実行方法

### 動作確認（Backlog書き込みなし）

```bash
python main.py --run-now --dry-run
```

### 今すぐ実行（本番）

```bash
python main.py --run-now
```

### スケジューラー起動（毎週金曜18時に自動実行）

```bash
python main.py
```

> サーバーやクラウドで常時起動させる場合は後述の「本番運用」を参照

---

## 5. 出力例

### ローカルファイル（output/weekly_report_20250620.md）

```markdown
## 週次進捗報告 2025/06/16〜2025/06/20

---

### 📋 Backlog 対応状況

**【EC推進プロジェクト】**
- ECPROJ-123 商品マスタ更新対応（処理中）
  - コメント: 商品コードの重複チェックロジックを修正しました...

**【SALES_TEAM】**
- SALES-45 週次KPIレポート作成（完了）

### 💬 Slack コミュニケーション

**#ec-project**（5件の発言）
  - 商品マスタの件、確認しました。明日中に対応します...
  - （他 2 件）

### 📅 工数サマリー（Googleカレンダーより）

**週間合計工数: 32.5時間**

**06/16(Mon)** (8.0h)
  - 週次定例MTG（1.0h）
  - EC案件打ち合わせ（1.5h）
  ...
```

### Backlogへの転記

- 親課題が存在する場合 → 該当する親課題にコメント追加
- 親課題がない場合 → `週次活動報告 2025/06/20` として新規起票

---

## 6. 本番運用（常時起動）

### GitHub Actions を使う場合（無料・推奨）

`.github/workflows/weekly_report.yml` を作成：

```yaml
name: Weekly Report
on:
  schedule:
    - cron: '0 9 * * 5'  # 毎週金曜 18:00 JST (UTC 09:00)
  workflow_dispatch:      # 手動実行も可能

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python main.py --run-now
        env:
          BACKLOG_API_KEY: ${{ secrets.BACKLOG_API_KEY }}
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          # Google認証はサービスアカウントキーをSecretに保存
```

### cron を使う場合（ローカルサーバー）

```bash
# crontab -e で以下を追加
0 18 * * 5 cd /path/to/weekly_report && python main.py --run-now >> output/cron.log 2>&1
```

---

## 7. トラブルシューティング

| エラー | 原因 | 対処 |
|---|---|---|
| `401 Unauthorized` | APIキーが無効 | Backlog/Slack のキーを再確認 |
| `not_in_channel` | BotがSlackチャンネルに未参加 | `/invite @Bot名` で招待 |
| Google認証エラー | credentials.jsonが古い | Google Consoleで再発行 |
| Claude APIエラー | 権限なし/残高不足 | `claude.enabled: false` にしてルールベースで動作 |

---

## 8. ファイル構成

```
weekly_report/
├── main.py                        # エントリーポイント
├── requirements.txt               # 依存パッケージ
├── config/
│   ├── config.yaml                # 設定ファイル（要編集）
│   ├── google_credentials.json    # Google OAuthキー（要配置）
│   └── google_token.pickle        # 自動生成（初回認証後）
├── src/
│   ├── backlog_client.py          # Backlog APIクライアント
│   ├── slack_client.py            # Slack APIクライアント
│   ├── google_calendar_client.py  # Google Calendar APIクライアント
│   ├── report_generator.py        # レポート生成（AI/ルールベース）
│   └── backlog_poster.py          # Backlog転記処理
└── output/
    ├── weekly_report_YYYYMMDD.md  # 自動生成レポート
    └── run.log                    # 実行ログ
```
