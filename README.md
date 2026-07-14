# Wasabi

> Backlog / Slack / Google Meet 議事録を横断収集し、Gemini AI で要約・蓄積・活用する業務 AI アシスタント

---

## What is this?

Backlog・Slack・Google Meet 議事録を自動収集し、Gemini AI で要約・整形・ベクトル化して蓄積する**業務情報の収集・蓄積・活用を自動化する AI アシスタント**。

週次 Backlog 転記にとどまらず、ナレッジ検索・日次サマリー・チケットアラート・Backlog 課題の起票まで、日常の業務支援を一元的に担います。

情報の流れを「収集 → 蓄積・整形 → 活用」の3段階で自動化することで、散在する情報の確認・整理にかかる手作業を排除します。

---

### ① 情報収集（自動）

| 情報源 | 手動（今まで） | このシステム |
|---|---|---|
| Backlog チケット | 担当・関連チケットを手動で確認 | 担当・作成・コメントチケットを自動取得 |
| Slack | チャンネルを横断して手動確認 | 自分の発言・参加スレッドを自動収集 |
| Google Meet 議事録 | 会議後にドキュメントを手動確認 | カレンダー添付 / Meet フォルダから自動取得 |
| 進捗メモ（手動インプット） | 週次実行まで反映できない | Slack Bot に送ったメモを即座に情報源へ追加 |

### ② 蓄積・整形（AI）

| 処理 | 手動（今まで） | このシステム |
|---|---|---|
| 週次レポート生成 | 各情報源を確認しながら自分で文章化 | Gemini が収集データを要約し、Backlog 親課題へ自動転記（毎週金曜 18:00） |
| ナレッジ蓄積 | 議事録・チケット・Slack が散在したまま | Firestore へ自動蓄積・Firestore Vector Search でベクトルインデックス化 |
| 親課題の紐付け | どのチケットに書くか自分で判断 | チャンネル設定 → Backlog API 遡及 → Gemini 推測の3段階で自動判別 |

### ③ 活用（Bot・アラート）

| 機能 | 手動（今まで） | このシステム |
|---|---|---|
| 過去情報の検索 | Backlog・Slack・ドキュメントを個別に手動検索 | Slack Bot に質問 → KB を意味検索して Gemini が回答（RAG・出典リンク付き・スレッド文脈継続） |
| 案件の経緯把握 | チケットのコメントを遡って読む | `経緯 課題キー` で時系列サマリーを自動生成（引き継ぎ・報告用） |
| KB の閲覧 | — | 管理 WebApp の KB 探索ページで一覧・検索・全文表示 |
| 未対応チケットの把握 | 定期的に Backlog を手動確認 | N 営業日更新なしのチケットを毎朝 Slack DM で自動通知 |
| 当日の活動サマリー | 振り返りは自分で行う | 毎日 17:30 に当日の活動を Gemini が整形して Slack DM 通知 |

---

## How it's built

### 技術スタック

| 用途 | 技術 |
|---|---|
| バッチ処理 | Python + `schedule` ライブラリ（ローカル常駐 or タスクスケジューラ） |
| AI エンジン | Gemini API（`gemini-2.5-flash` / 埋め込み: `gemini-embedding-001`） |
| Backlog 連携 | Backlog REST API v2 |
| Slack 連携 | Slack SDK（Bot Token）+ Slack Bolt（Socket Mode Bot） |
| Google 連携 | Google Calendar / Drive / Docs API（OAuth2） |
| データ永続化 | SmartSync Firestore（andst-hd-ax）REST API（gRPC 不使用・`requests` ベース） |
| 意味検索 | Firestore Vector Search（findNearest）+ Gemini Embeddings（768次元） |
| チーム設定 | Firestore `wasabi_teams` コレクション（管理 WebApp / Bot から編集） |
| 管理画面 | FastAPI + Jinja2（`webapp/`・Cloud Run + IAP 想定） |
| 設定管理 | `config/config.yaml`（システム設定）+ `.env`（シークレット） |

### データストア（SmartSync Firestore / andst-hd-ax）

すべて SmartSync Firestore（検証: `smart-sync-stg` / 本番: `smart-sync`）に保存する。

| コレクション | 内容 |
|---|---|
| `context_snapshots` | KB（`wasabi_{team_id}_*` doc_id・embedding 付き）※SmartSync と共用 |
| `wasabi_teams` | チームプロファイル（転記先・チャンネルマッピング・メンバー等） |
| `wasabi_admins` | 管理 WebApp の編集権限（メールアドレス） |
| `wasabi_weekly_reports` / `wasabi_calendar_reports` | 週次・カレンダーレポート |
| `wasabi_manual_memos` / `wasabi_shared_infos` | Bot 経由の手動メモ・共有事項 |
| `wasabi_sync_logs` / `wasabi_job_status` / `wasabi_audit_logs` | 実行ログ・鮮度管理/ジョブロック・監査 |
| `wasabi_pending_posts` / `wasabi_reply_ts` | Bot の状態永続化（転記プレビュー・削除用 ts）。再起動・マルチインスタンス対応 |
| `wasabi_qa_logs` | Bot への質問ログ + 👍👎 フィードバック（管理画面「利用状況」で集計・KB ギャップ発見に使用） |

### 設計方針

- **Gemini フォールバック**: `gemini.enabled: false` でルールベース要約に自動フォールバック
- **3段階の親課題判別**: `channel_mapping` 明示指定 → Backlog API で確定的解決 → Gemini 推測
- **Hallucination 抑止**: データソースが少量の場合に「これ以外の情報は存在しない」旨をプロンプトに明示。Gemini が存在しない会議・決定事項を生成するのを防ぐ
- **議事録キーワードフィルタ**: `related_meeting_keywords` で全体MTGの議事録から関連議題のみを抽出し、無関係な他プロジェクトの内容の混入を防ぐ
- **矛盾チェック**: 複数親課題への転記内容を Gemini で横断チェック（Gemini 有効時）
- **Firestore REST**: 社内プロキシ環境の gRPC SSL 問題を回避するため、Firestore SDK を使わず REST API を直接呼び出す
- **データキャッシュ**: `--only report` / `--only kb` の2回目以降は `output/cache/YYYYMMDD.pkl` から即座にロードして API 呼び出しを省略
- **議事録要約並列化**: `pre_summarize_meetings()` で全議事録の Gemini 要約を `ThreadPoolExecutor(workers=10)` で並列実行し、以降の処理で `doc["_summary"]` を再利用
- **チームメンバー対応**: `team_members` を設定することで Backlog・Slack KB をチーム全員分収集

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
    SCH->>FS: Step 7b: 手動メモ（Slack Bot 経由）を取得して情報源に追加
    SCH->>BL: Step 8: 親課題を判別してコメント転記
    Note over BL,GEM: ① channel_mapping → ② Backlog API 遡及 → ③ Gemini 判別 → ④ 矛盾チェック
    SCH->>FS: Step 9: ナレッジベース生成（tickets / slack / meetings）+ Firestore Vector Search に embedding 保存
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
## Wasabi 週次進捗レポート YYYY/MM/DD〜YYYY/MM/DD

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
*このコメントは Wasabi により自動転記されました*
```

**Gemini 無効時（ルールベースフォールバック）**

```
## Wasabi 週次進捗レポート YYYY/MM/DD〜YYYY/MM/DD

---

■現在の進捗
・ISSUE-KEY 課題名（ステータス）
・#channel_name: 発言サンプル
・会議: 議事録タイトル

■リスク共有
特になし

---
*このコメントは Wasabi により自動転記されました*
```

**親課題の判別ロジック（4段階）**

| 優先度 | 方法 | 対象 |
|---|---|---|
| ① | `config.yaml` の `channel_mapping` で明示指定 | Slack チャンネル → 親課題 |
| ② | Backlog API で親課題チェーンを遡及（確定的） | Backlog 活動 → SALES_TEAM 親課題 |
| ③ | Gemini AI で判別（Gemini 有効時のみ） | ①②未対応の Slack チャンネル |
| ④ | Gemini で転記内容の矛盾チェック（Gemini 有効時のみ） | 全転記結果を横断検証 |

**議事録の関連フィルタリング**

`channel_mapping` に `related_meeting_keywords` を設定すると、全体MTG（販売チームMTG 等）の議事録を親課題へ紐付ける際にキーワードフィルタを適用します。議事録タイトル・要約・本文にキーワードを含む議事録のみを渡し、関係ない他プロジェクトの議題が転記内容に混入することを防ぎます。省略した場合はすべての議事録が対象になります。

**手動メモの反映**

Slack Bot に登録した進捗メモ（`メモ` コマンド）は、週次レポート実行時に `【手動メモ（担当者入力）】` セクションとして Gemini に渡され、転記内容に反映されます。

---

## Slack Bot

`python main.py --bot` で起動するインタラクティブ Bot。**チャンネル内のメンション**または **Bot との DM** で以下の操作ができます。

### コマンド一覧

| 操作 | コマンド例 | 備考 |
|---|---|---|
| KB 質問（RAG） | `ACE刷新の進捗は？` | 出典リンク付き回答。スレッド内・DM（30分以内）の追い質問は文脈を継続 |
| KB 質問（絞り込み） | `種別:議事録 期間:今週 ポスタスの議論` | 種別: 議事録/チケット/slack、期間: 今週/先週/今月/N日/M/D-M/D |
| 経緯サマリー | `経緯 SALES_TEAM-27` | Backlog 全コメント + KB から時系列まとめを生成（引き継ぎ・報告用） |
| 詳しい人（人ナビ） | `詳しい人 マケプレ` | KB の担当者・発言記録からトピックに詳しい人を推定。RAG が「該当なし」のときも自動で関係者を提示 |
| 進捗メモ登録 | `メモ SALES_TEAM-23: 内容` | 週次転記の情報源に追加 |
| 共有事項の起票 | `共有事項 タイトル: 内容` | Backlog に課題作成・投稿者を担当者に設定 |
| 情報収集（手動） | `情報収集` / `情報収集 7/1-7/7` | Backlog+Slack+議事録をまとめて KB 化。30分以内の再実行はスキップ・実行中は完了通知待ちに登録 |
| 議事録収集（手動） | `議事録収集` / `議事録収集 7/1-7/7` | 議事録のみ収集（カレンダー添付含む） |
| Backlog 転記（手動） | `転記` / `転記 7/1-7/4` | プレビュー表示 → 確認ボタンで実行。プレビューは Firestore に保存され Bot 再起動後も有効（1時間） |
| 設定確認 | `設定確認` | チーム設定の閲覧（全員可） |
| 転記 ON/OFF | `設定 転記 オン/オフ` | チーム管理者のみ |
| マッピング設定 | （対象チャンネル内で）`設定 マッピング SALES_TEAM-27` | チーム管理者のみ・Backlog 実在検証つき |
| Slack ID 確認 | `私のID` | メンバー登録用 |
| Bot 返信を削除 | 返信スレッドで `delete` | |
| ヘルプ表示 | `help` / `ヘルプ` | App Home タブにも機能一覧を常時表示 |

チャンネルでは `@Wasabi Bot` メンション、DM ではメンションなしで送信する。

### 管理 WebApp（webapp/）

チーム設定（`wasabi_teams`）を編集する管理画面。複雑な設定（チーム作成・マッピング一覧編集・メンバー管理・権限管理）は管理画面、簡易な切替は Bot の設定コマンドという分担。

```
# ローカル起動
cd webapp
DEBUG_MODE=true DEBUG_USER=<自分のメール> python -m uvicorn app.main:app --port 8765
# Cloud Run デプロイ（Cloud Shell 等 gcloud 環境から）
bash infra/deploy_wasabi_webapp.sh
```

- 認証: IAP（Cloud Run）/ `DEBUG_MODE`（ローカル）。編集は `wasabi_admins` 登録者のみ
- Backlog ユーザーの名前検索（数値 ID 自動入力）・課題キーの実在検証つき
- デプロイ済み URL: `https://wasabi-webapp-504720017092.asia-northeast1.run.app`（IAP 保護・min-instances=0）

**画面構成**

| ページ | 内容 | 権限 |
|---|---|---|
| `/`（ダッシュボード） | チーム一覧・KB 統計・新規チーム作成 | 閲覧: 全員 / 編集: wasabi_admins |
| `/teams/{id}`（チーム詳細） | 基本設定・転記方式・チャンネルマッピング（インライン編集）・メンバー管理 | 同上 |
| `/kb`（KB 探索） | KB ドキュメントの一覧（種別タブ）・意味検索・全文表示・原文リンク | 全員（読み取り専用） |
| `/qa`（利用状況） | 質問数・該当なし率・フィードバック集計、答えられなかった質問の一覧 | 全員（読み取り専用） |

※ KB 意味検索は `GEMINI_API_KEY`（Secret Manager: `wasabi-gemini-api-key`）が必要。未設定環境では一覧のみ動作

### 進捗メモの詳細

Slack Bot に登録したメモは Firestore `wasabi_manual_memos` コレクションに保存され、次の週次レポート実行時（金曜 18:00）に情報源として使用されます。

```
# 課題キーを指定する場合（その親課題の転記に反映）
@Wasabi Bot メモ SALES_TEAM-23: ポスタスの検証環境エラーは解消済み。6/27 本番反映予定

# 課題キーを省略する場合（Gemini が本文から最も関連する親課題を1件判別して転記）
# 判別不能な場合は転記対象外になります
@Wasabi Bot メモ ポスタスの検証環境エラーが解消した
```

Bot が確認メッセージを返します：
```
✅ 進捗メモを保存しました
・課題: SALES_TEAM-23
・内容: ポスタスの検証環境エラーは解消済み。6/27 本番反映予定
次の週次レポート実行時に情報源として使用されます。
```

`メモ` の代わりに `memo` `進捗メモ` `手動メモ` `進捗入力` でも動作します。

### DM の開始方法

1. Slack の左サイドバーで **Wasabi**（Bot名）を検索
2. Bot 名をクリック → **「メッセージを送る」**
3. そのままテキストを送信（メンション不要）

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

KB 生成で作成されるドキュメントは Firestore `context_snapshots` コレクションに永続保存され、同時に Firestore Vector Search へベクトル化（768次元）して蓄積されます。ローカルへのファイル保存は一切行いません。

### Firestore コレクション構造

| コレクション | ドキュメントID形式 | 内容 | TTL |
|---|---|---|---|
| `context_snapshots` | `wasabi_{team_id}_backlog_{ISSUE-KEY}` | チケット要約・コメント履歴（SmartSync 形式） | なし（永続） |
| `context_snapshots` | `wasabi_{team_id}_slack_{channel_id}` | Slack チャンネル週次まとめ | なし（永続） |
| `context_snapshots` | `wasabi_{team_id}_meeting_{slug}` | 議事録要約 | なし（永続） |
| `wasabi_manual_memos` | `memo_{YYYYMMDD}_{timestamp_ms}` | Slack Bot 経由の手動メモ | なし（永続） |
| `wasabi_shared_infos` | `shared_{YYYYMMDD}_{timestamp_ms}` | 共有事項コマンド経由の起票内容 | なし（永続） |
| `wasabi_sync_logs` | （自動採番） | 実行ログ | expire_at 90日 |

**差分スキップ**: チケット KB はすでに保存済みの `backlog_updated_at` と比較し、変更がなければ API 呼び出しをスキップします。

### 並列処理の構成

| 処理 | workers | 備考 |
|---|---|---|
| 議事録要約（Gemini） | 10 | `pre_summarize_meetings()` で事前並列実行 |
| チケット KB | 8 | 各チケット内で `get_issue` + `get_all_comments` をさらに2並列 |
| Slack KB | 3 | Slack API レート制限を考慮して控えめ |
| 議事録 KB | 10 | 事前要約済みのため Gemini 呼び出しなし |

### チームメンバー対応

`team_members` を設定すると、KB 生成時に自分以外のメンバーの活動も収集・蓄積します。週次レポートの Backlog 転記は引き続き個人分のみ。

```yaml
team_members:
  - name: "田中"
    backlog_user_id: 789012
    slack_user_id: "U02BCDEFGH"
  - name: "佐藤"
    backlog_user_id: 345678
    slack_user_id: "U03CDEFGHI"
```

| 対象 | 個人モード | チームモード |
|---|---|---|
| Backlog KB | 自分が関わったチケット | 全メンバーが関わったチケット（統合・重複排除） |
| Slack KB | 自分の投稿・参加スレッド | 全メンバーの投稿（★ = 自分、名前表示 = 他メンバー） |
| 議事録 KB | 自分が参加した MTG（OAuth制約） | 変更なし |

### 意味検索（Firestore Vector Search + ハイブリッド）

KB 保存と同時に Gemini Embeddings（768次元）でベクトル化し、Firestore の `embedding` フィールドとして保存します。ローカルファイルは使用しません。

検索は以下を組み合わせたハイブリッド方式:

1. **課題キー直接ヒット**: クエリに課題キー（`SALES_TEAM-27` 等）が含まれる場合、該当 KB ドキュメントを直接取得して必ず結果の先頭（score=1.0）に配置
2. **キーワード全文スキャン**: Gemini でクエリから検索語を展開（例: マケプレ→マーケットプレイス）し、KB を全文スキャン（embedding は転送しない軽量版）。長い集約テキスト中の一言及などベクトル検索が拾えないケースを補完し、マッチ箇所周辺の抜粋を Gemini に渡す（score=0.9）
3. **ベクトル検索**: findNearest（COSINE）による意味検索
4. **フィルタ**: `source_type`（種別）と日付（backlog=更新日 / meeting=開催日 / その他=同期日）で絞り込み。フィルタ指定時は 4 倍 over-fetch してクライアント側で絞る

収集ジョブ（情報収集・週次実行）の最後に、**embedding 未設定のドキュメント（SmartSync の日次同期分を含む）を自動でベクトル化**して検索対象に含める。

回答には出典ブロックを自動付与する（Backlog 課題リンク・Slack チャンネルリンク・議事録の Google Docs リンク）。議事録リンクは KB の `source_url` フィールド（収集時に Docs ID から生成）を使用。

```bash
# 意味検索（--only kb 実行後から利用可）
python main.py --search "ACE刷新の進捗"

# Firestore の既存ドキュメントから embedding を一括再生成
python main.py --sync-vectors

# RAG 検索のゴールデンクエリ回帰テスト（実 API 使用・検索まわりの改修時に実行）
RUN_GOLDEN=1 python -m pytest tests/test_rag_golden.py -v
```

---

## Directory Structure

```
Wasabi/
├── main.py                         # エントリーポイント・スケジューラ
├── requirements.txt                # 依存パッケージ
├── .env                            # APIキー類（要作成・Git管理外）
├── config/
│   ├── config.yaml                 # システム設定（要編集）
│   ├── google_credentials.json     # Google OAuth クライアントキー（要配置）
│   ├── google_token.pickle         # 自動生成（初回認証後）
│   └── andst-hd-ax-*.json          # SmartSync GCP サービスアカウントキー（要配置・Git管理外）
├── src/
│   ├── backlog_client.py           # Backlog REST API v2（スロットリング・APIキーマスク付き）
│   ├── backlog_poster.py           # 親課題判別・コメント転記・手動メモ統合
│   ├── slack_client.py             # Slack SDK クライアント
│   ├── slack_bot.py                # Slack Bot（RAG・経緯・手動収集/転記・設定コマンド・App Home）
│   ├── bot_state.py                # Bot 状態の Firestore 永続化（ロック・プレビュー・削除ts）
│   ├── manual_jobs.py              # Bot からの手動実行（収集・転記・鮮度チェック・embedding補完）
│   ├── google_calendar_client.py   # Google Calendar API クライアント
│   ├── google_docs_client.py       # Google Drive / Docs API クライアント（議事録取得）
│   ├── gemini_client.py            # Gemini API（要約・判別・RAG・経緯サマリー・埋め込み）
│   ├── report_generator.py         # レポート生成（Gemini / ルールベース）・議事録並列要約
│   ├── knowledge_base.py           # ナレッジベース生成（SmartSync Firestore へ直接保存）
│   ├── smartsync_client.py         # SmartSync Firestore REST クライアント（Vector Search 込み）
│   ├── smartsync_convert.py        # Wasabi 形式 → SmartSync 形式の変換
│   ├── smartsync_store.py          # 週次レポート・メモ等の保存（wasabi_* コレクション）
│   ├── team_config.py              # チームプロファイル（wasabi_teams）の読み書き
│   ├── vector_store.py             # ハイブリッド検索（Vector Search + 課題キー直接ヒット + フィルタ）
│   ├── firestore_client.py         # 【DEPRECATED】移行スクリプト用に残置
│   ├── ticket_alert.py             # 未対応チケット警告
│   ├── daily_summary.py            # 日次夕方サマリー
│   └── cleanup.py                  # 転記済みコメント・課題の削除ツール
├── webapp/                         # 管理 WebApp（FastAPI・Cloud Run + IAP）
│   ├── app/
│   │   ├── main.py / deps.py       # FastAPI 初期化・IAP 認証
│   │   ├── routers/                # pages / teams（設定 CRUD）/ kb（KB 探索）
│   │   └── templates/              # index / team / kb
│   ├── Dockerfile
│   └── requirements.txt
├── infra/
│   ├── deploy_wasabi_webapp.sh     # WebApp の Cloud Run デプロイ
│   ├── cloudbuild_webapp.yaml
│   └── create_smartsync_vector_index.py  # Vector Index 作成（gcloud 不要）
├── scripts/
│   ├── init_wasabi_teams.py        # チームプロファイル初期化（config.yaml → Firestore）
│   ├── migrate_to_smartsync.py     # 旧 KB データの移行（使い捨て）
│   ├── migrate_misc_to_smartsync.py
│   └── copy_stg_to_prod.py         # stg → 本番の wasabi_* データコピー（Phase 1 移行用）
├── tests/                          # pytest テスト
├── docs/                           # 要件定義・説明資料（docx 含む）
└── output/                         # 生成物（Git管理外）
    ├── cache/YYYYMMDD.pkl          # 当日データキャッシュ（--only report/kb で再利用）
    └── run.log                     # 実行ログ（ローテーション付き）
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
SLACK_APP_TOKEN=xapp-1-xxxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxxxxxx   # Slack Bot 用（Socket Mode）
GEMINI_API_KEY=AIzaSy...
SMARTSYNC_GOOGLE_APPLICATION_CREDENTIALS=config/andst-hd-ax-xxxx.json
SMARTSYNC_FIRESTORE_DATABASE=smart-sync-stg   # 本番: smart-sync
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
| `im:history` | DM メッセージ受信（Slack Bot 用） |
| `im:write` | DM 送信（日次サマリー・Bot 返信用） |
| `chat:write` | チャンネルへのメッセージ送信・Bot 返信 |
| `app_mentions:read` | チャンネル内のメンション受信（Slack Bot 用） |

3. ワークスペースにインストールし、Bot Token (`xoxb-...`) を `.env` に設定
4. プライベートチャンネルには `/invite @ボット名` で招待

**自分の Slack User ID 確認**：Slack プロフィール → 「…」→「メンバー ID をコピー」

#### Slack Bot の追加設定（`--bot` コマンド用）

1. **Socket Mode** を有効化 → **App-Level Token** を発行（スコープ: `connections:write`） → `SLACK_APP_TOKEN` に設定
2. **Event Subscriptions** → Bot Events に以下を追加：

| イベント | 用途 |
|---|---|
| `app_mention` | チャンネル内のメンション受信 |
| `message.im` | DM の受信 |

3. **App Home** → **Messages Tab** → **Allow users to send Slash commands and messages from the messages tab** をオン（DM を有効化）

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

### 7. SmartSync Firestore の設定（GCP）

データストアは SmartSync チームの GCP プロジェクト `andst-hd-ax` の Firestore を使用します
（検証: `smart-sync-stg` / 本番: `smart-sync`）。

> **注意**: 本ツールの Firestore クライアントは gRPC SDK を使用せず、REST API を直接呼び出します。社内プロキシ環境での `SSL_ERROR_SSL` を回避するため、Python 標準の `requests`（urllib3 + certifi）で通信します。

#### 7-1. サービスアカウントキーの配置（ローカル実行用）

`andst-hd-ax` プロジェクトのサービスアカウントキー（JSON）を `config/` に配置し、
`.env` に以下を設定します（キーは `.gitignore` 済み・コミット禁止）:

```
SMARTSYNC_GOOGLE_APPLICATION_CREDENTIALS=config/andst-hd-ax-xxxx.json
SMARTSYNC_FIRESTORE_DATABASE=smart-sync-stg
```

Cloud Run 上では ADC（サービスアカウント）で自動認証されるためキーは不要です。

#### 7-2. Vector Index の作成（初回のみ）

Firestore Vector Search には `context_snapshots.embedding` の Vector Index が必要です:

```bash
python infra/create_smartsync_vector_index.py --list        # 既存確認
python infra/create_smartsync_vector_index.py               # 作成（gcloud 不要）
```

#### 7-3. チームプロファイルの初期化（初回のみ）

```bash
python scripts/init_wasabi_teams.py --admin <管理者メール>
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
      related_meeting_keywords:          # 議事録フィルタキーワード（省略可）
        - "ストアアプリ"                   # 全体MTGの議事録からこのキーワードを含む行のみ抽出
        - "hblab"                        # 無関係な議題の混入を防ぐ

slack_bot:
  enabled: true
  app_token: "${SLACK_APP_TOKEN}"        # xapp-... （Socket Mode用）
  n_results: 5                           # 1回の質問で参照する KB ドキュメント数
  shared_info:
    project_key: "SALES_TEAM"
    issue_type_id: 915353                # 共有事項の種別ID（要確認）

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

# チームメンバー（省略可）: 設定するとKBをチーム全員分収集
team_members:
  - name: "田中"
    backlog_user_id: 789012
    slack_user_id: "U02BCDEFGH"

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
| `kb` | ナレッジベース生成（Firestore `context_snapshots` への保存 + Firestore Vector Search への embedding 保存） |

> **データキャッシュ**: `--only report` / `--only kb` は初回実行時に Backlog・Slack・Calendar・議事録のデータを `output/cache/YYYYMMDD.pkl` に保存します。2回目以降は API 呼び出しをスキップしてキャッシュから即座にロードします（当日中有効）。

### 意味検索 + RAG 回答

KB に蓄積されたデータを自然言語で検索し、Gemini が回答を生成します（RAG）。`--only kb` を少なくとも1回実行してインデックスを構築してから使用してください。

```bash
python main.py --search "ACE刷新の進捗は？"
python main.py --search "ポスタス検証環境のエラーは解消したか"
```

出力は「参照ドキュメント（上位5件）」と「Gemini 回答」の2セクション構成です。

### Slack Bot 起動（KB 質問応答 + 進捗メモ登録）

```bash
python main.py --bot
```

起動後、Slack チャンネルでのメンションまたは Bot との DM で以下が使えます。

| 操作 | チャンネル | DM |
|---|---|---|
| KB 質問 | `@Wasabi Bot ACE刷新の進捗は？` | `ACE刷新の進捗は？` |
| 進捗メモ | `@Wasabi Bot メモ SALES_TEAM-23: 内容` | `メモ SALES_TEAM-23: 内容` |
| Bot 返信削除 | 返信スレッドで `@Wasabi Bot delete` | `delete` |
| ヘルプ | `@Wasabi Bot help` | `help` |

> Bot は常駐プロセスとして動作します。スケジューラ（`python main.py`）と同時に使いたい場合は別ターミナルで起動してください。

### Firestore ベクトル全件再同期

Firestore に既存ドキュメントがある状態で embedding を一括再生成する場合に使用します（初回セットアップ時や embedding モデル変更後など）。

```bash
python main.py --sync-vectors
```

### クリーンアップ（転記内容の削除）

動作確認後にテスト転記を削除したい場合に使用します。

```bash
python main.py --cleanup
```

起動すると対話メニューが表示されます。

```
============================================================
  Wasabi クリーンアップツール
============================================================

操作を選んでください:
  1. コメントを削除（親課題へのコメント転記分）
  2. 課題を削除（Wasabi が起票した課題）
  q. 終了

> 1

プロジェクトの親課題を検索中...

2 件の Wasabi コメントが見つかりました:

  [ 1] SALES_TEAM-27 「店舗ACE刷新」  2026/06/24 18:02
       ## Wasabi 週次進捗レポート 2026/06/22〜2026/06/24...

  [ 2] SALES_TEAM-254 「6/12発生した障害」  2026/06/24 18:03
       ## Wasabi 週次進捗レポート 2026/06/22〜2026/06/24...

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
| `--search "クエリ"` | KB を意味検索し Gemini が自然言語で回答（RAG） |
| `--bot` | Slack Bot を Socket Mode で起動（KB 質問応答・進捗メモ登録） |
| `--sync-vectors` | Firestore の `context_snapshots` 全件の embedding を再生成して Firestore Vector Search に保存 |
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
| `ModuleNotFoundError: slack_bolt` | slack-bolt 未インストール | `pip install -r requirements.txt` を再実行 |
| `--search` で結果が0件 | Firestore に embedding が未保存 | `--only kb` または `--sync-vectors` を先に実行 |
| `--search` で結果が0件（インデックス作成直後） | Firestore Vector Index がビルド中 | 数分待ってから再実行 |
| Gemini 判別の誤分類 | 関連性が低い内容が混入 | `channel_mapping` で明示マッピングを追加 |
| Bot DM に「メッセージ送信はオフ」と表示 | App Home の Messages Tab が無効 | Slack App → **App Home** → **Messages Tab** をオン |
| `--bot` で Bot が応答しない | Event Subscriptions 未設定 | `app_mention` / `message.im` を Bot Events に追加 |

---

## Testing

```bash
python -m pytest tests/ -v
```

---

## Related

- [docs/requirements_v2.md](docs/requirements_v2.md) — 詳細要件定義（v2.1）
