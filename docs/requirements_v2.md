# 週次進捗報告・ナレッジ管理ツール 要件定義書

| 項目 | 内容 |
|---|---|
| 版 | 2.1 |
| 作成日 | 2026-06-23 |
| 対象者 | ta.kimura@andst-hd.co.jp |

---

## 1. 現状ツールのまとめ

### できること

| # | 機能 | タイミング |
|---|---|---|
| F01 | 週次進捗レポート自動生成・Backlog転記 | 毎週金曜 18:00 |
| F02 | 活動ナレッジ蓄積（チケット別・Slack別） | 週次と同時 |
| F03 | 未対応チケット警告通知（Slack DM） | 毎日 09:00（土日祝スキップ） |
| F04 | 日次夕方サマリー通知（Slack DM） | 毎日 17:30（土日祝スキップ） |

### データソース

| ソース | 取得内容 | 認証方式 |
|---|---|---|
| Backlog | 担当・作成・コメントチケット、活動履歴 | APIキー |
| Slack | 自分の発言・参加スレッド全体 | Bot Token |
| Google Calendar | イベント・参加状況・工数 | OAuth2 |

### アウトプット

| 出力先 | 内容 |
|---|---|
| Backlog コメント | 週次進捗レポート（親課題へ自動投稿） |
| `output/weekly_report_YYYYMMDD.md` | 週次レポート全文（ローカル保存） |
| `output/calendar_report_YYYYMMDD.md` | カレンダー工数サマリー |
| `output/knowledge/tickets/TICKET-KEY.md` | チケット別対応履歴 |
| `output/knowledge/slack/YYYYWW_channel.md` | チャンネル別Slackスレッド |
| Slack DM（自分宛） | 未対応チケット警告 / 日次サマリー |

---

## 2. 新機能要件：Gemini議事録のデータソース追加

### 2-1. 背景・目的

Google Meet + Gemini によって自動生成される議事録をツールに取り込み、  
以下を実現する。

- 会議内容をナレッジとして自動蓄積する
- 会議での決定事項・アクションアイテムを週次レポートに反映する
- Backlog チケットとの紐付けにより、会議起点の作業管理を容易にする

### 2-2. 議事録の格納場所

| 項目 | 内容 |
|---|---|
| 格納場所 | Google Drive > マイドライブ > Meet Recordings |
| フォルダID | `1dqz9mGm2eCrThWJBzuiee3TPBw1nOMXj` |
| ファイル形式 | Google Docs（`application/vnd.google-apps.document`） |
| 生成者 | Gemini（Google Meet の自動文字起こし・要約機能） |
| ファイル名規則 | Google Meet が自動付与（例: `2026-06-23 販売チーム週次定例`） |

### 2-3. 新規機能一覧

| # | 機能名 | 概要 | 実行タイミング |
|---|---|---|---|
| F05 | 議事録収集 | Drive API で `Meet Recordings` フォルダを取得 | 週次レポートと同時 |
| F06 | ナレッジ蓄積（議事録） | 会議単位で Markdown に保存 | F05 と同時 |
| F07 | 週次レポートへの統合 | 議事録の要点を週次レポートの会議セクションに追加 | F05 と同時 |

### 2-4. 機能詳細

#### F05: 議事録収集

| 項目 | 仕様 |
|---|---|
| 対象フォルダ | `Meet Recordings`（フォルダID: `1dqz9mGm2eCrThWJBzuiee3TPBw1nOMXj`） |
| 取得対象 | 対象週（月〜金）に作成された Google Docs ファイル |
| 判別条件 | MIMEタイプ = `application/vnd.google-apps.document` かつ `createdTime` が対象週内 |
| 取得内容 | タイトル・作成日時・本文全文 |
| エラー時 | 警告ログを出してスキップ（週次レポート全体には影響させない） |

#### F06: ナレッジ蓄積（議事録）

| 項目 | 仕様 |
|---|---|
| 出力先 | `output/knowledge/meetings/YYYYMMDD_会議名.md` |
| 会議名 | Google Docs のタイトルをそのまま使用（30文字でトランケート） |
| 更新方式 | 毎週上書き（既存ファイルがあれば上書き） |
| ファイル構成 | 会議名・日時・本文全文 |

Markdownフォーマット例：
```markdown
# 2026-06-23 販売チーム週次定例

**日時:** 2026-06-23  
**ソース:** Google Meet / Gemini 自動生成

---

（本文全文）
```

#### F07: 週次レポートへの統合

| 項目 | 仕様 |
|---|---|
| 追加セクション | `### 📝 会議・決定事項（Google Meet）` |
| 掲載内容 | 会議名・日時・本文冒頭200文字（超過分はトランケート） |
| Gemini API 有効時 | 本文全体を渡して決定事項・アクションアイテムを要約 |
| Gemini API 無効時 | 本文冒頭200文字をそのまま掲載（ルールベース） |

### 2-5. AI要約機能（Gemini API）

議事録の要約には、Claude API ではなく **Gemini API（Google AI Studio）** を使用する。  
Google のサービス（Drive / Docs）と同じ認証基盤で統一でき、追加の API キー管理コストが低い。

| 項目 | 仕様 |
|---|---|
| 使用モデル | `gemini-2.0-flash`（デフォルト） |
| SDK | `google-generativeai`（`pip install google-generativeai`） |
| APIキー管理 | `.env` に `GEMINI_API_KEY` として保存 |
| 用途 | 議事録の決定事項・アクションアイテム抽出 / 週次レポート要約 |
| 無効時の挙動 | `gemini.enabled: false` で全機能がルールベースにフォールバック |

プロンプト設計方針：
- 入力：議事録の本文全文
- 出力：「決定事項」「アクションアイテム（担当者・期限）」「議題サマリー」の3セクション
- 出力形式：Markdown（Backlog コメントに直接貼り付け可能な形式）

### 2-6. 設定項目（config.yaml 追加分）

```yaml
google_meet:
  enabled: true
  # Meet Recordings フォルダの Google Drive フォルダID
  folder_id: "1dqz9mGm2eCrThWJBzuiee3TPBw1nOMXj"

gemini:
  api_key: "${GEMINI_API_KEY}"   # .env の GEMINI_API_KEY に設定
  enabled: false                 # true にすると AI 要約を使用（APIキー取得後に有効化）
  model: "gemini-2.0-flash"
```

`.env` への追加：
```
GEMINI_API_KEY=（Google AI Studio から取得したAPIキー）
```

### 2-7. システム構成（変更差分）

#### 新規ファイル

| ファイル | 役割 |
|---|---|
| `src/google_docs_client.py` | Google Drive API / Docs API のラッパー（議事録取得） |
| `src/gemini_client.py` | Gemini API のラッパー（議事録要約） |

#### 既存ファイルへの変更

| ファイル | 変更内容 |
|---|---|
| `config/google_credentials.json` | OAuth スコープに `drive.readonly` / `documents.readonly` を追加するため再作成が必要 |
| `config/config.yaml` | `google_meet` セクション・`gemini` セクションを追加 |
| `.env` | `GEMINI_API_KEY` を追加 |
| `requirements.txt` | `google-generativeai` を追加 |
| `src/knowledge_base.py` | `_generate_meeting_knowledge()` メソッドを追加 |
| `src/report_generator.py` | `build_backlog_comment()` に会議セクションを追加、Gemini 要約呼び出しを追加 |
| `main.py` | `run_weekly_report()` の Step 4 として議事録収集を追加、`GeminiClient` の初期化を追加 |

#### 処理フロー（週次レポート実行時）

```
Step 1: Backlog データ収集
Step 2: Slack データ収集
Step 3: Google Calendar データ収集
Step 4: Google Meet 議事録収集          ← NEW
Step 5: レポート生成
        ├── 議事録セクション追加          ← 変更
        └── Gemini API で要約（有効時）  ← NEW
Step 6: Backlog 転記
Step 7: ナレッジ蓄積
        ├── tickets/
        ├── slack/
        └── meetings/                   ← NEW
```

### 2-8. 前提条件・制約

| 項目 | 内容 |
|---|---|
| Google Workspace | Gemini の議事録自動生成機能が有効化されていること |
| Google Cloud Console | Drive API・Google Docs API を有効化すること |
| OAuth スコープ追加 | `https://www.googleapis.com/auth/drive.readonly`<br>`https://www.googleapis.com/auth/documents.readonly` |
| 再認証 | スコープ変更のため `config/google_token.pickle` を削除して再認証が必要 |
| Gemini API キー | Google AI Studio（`aistudio.google.com`）から取得し `.env` に設定 |
| 初期状態 | `gemini.enabled: false` で起動。APIキー取得後に `true` に変更して有効化 |

---

## 3. 実装優先順位

| 優先度 | 対応内容 | 備考 |
|---|---|---|
| 高 | Slack `chat:write` スコープ追加 | DM通知が機能していないため（既存バグ） |
| 高 | `google_docs_client.py` 実装 | F05〜F07 の基盤 |
| 高 | `knowledge_base.py` への議事録対応追加 | F06 |
| 中 | `report_generator.py` への会議セクション追加 | F07 |
| 中 | `gemini_client.py` 実装 | AI要約の基盤（無効状態で実装） |
| 低 | Gemini API 有効化（`enabled: true`） | APIキー取得後に設定変更のみ |
