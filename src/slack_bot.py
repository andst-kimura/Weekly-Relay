"""
Slack Bot - KB への自然言語質問に答えるインタラクティブ Bot
Socket Mode で動作するため、パブリック URL 不要。

起動: python main.py --bot
対応イベント:
  - app_mention: チャンネル内でメンションされた質問
  - message (im): DM で送られた質問

コマンド一覧:
  質問      : @Wasabi Bot ACE刷新の進捗は？
  進捗メモ  : @Wasabi Bot メモ [ISSUE-KEY]: 内容
  共有事項  : @Wasabi Bot 共有事項 [タイトル]: 内容
  削除      : 返信スレッドで @Wasabi Bot delete
"""
import logging
import re

logger = logging.getLogger(__name__)

_DELETE_COMMANDS = {"delete", "削除", "del", "消して", "消去"}
_MEMO_PREFIXES = ("メモ", "memo", "進捗メモ", "手動メモ", "進捗入力")
_SHARED_INFO_PREFIXES = ("共有事項", "共有", "shared")
# 課題キーのパターン（例: SALES_TEAM-23, MOBILEPOS-45）
_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
# 日付パターン（期限抽出用）: YYYY/MM/DD, YYYY-MM-DD, MM/DD, M/D
_DATE_RE = re.compile(
    r"(?:(?P<y>\d{4})[/-])?(?P<m>\d{1,2})[/-](?P<d>\d{1,2})"
)

_HELP_TEXT = """\
*Wasabi Bot* にようこそ！

*KB 質問（KB 検索 + AI 回答）*
　`@Wasabi Bot ACE刷新の進捗は？`

*進捗メモの登録（週次レポートの情報源に追加）*
　`@Wasabi Bot メモ SALES_TEAM-23: ポスタスのエラーは解消済み。6/27本番反映予定`
　`@Wasabi Bot メモ ポスタスの検証環境のエラーが解消した`（課題キー省略可）

*共有事項の起票（Backlog に「共有事項」課題を作成）*
　`@Wasabi Bot 共有事項 ACE刷新の日程変更について: 6/30→7/7に延期が決定しました`
　`@Wasabi Bot 共有事項: タイトルを省略した場合は本文冒頭が件名になります`

*Bot 返信の削除（チャンネル）*
　返信スレッド内で `@Wasabi Bot delete` と送信

*使い方（DM）*
　上記コマンドをメンションなしで送信。削除は `delete` と入力。

KB が空の場合は `python main.py --only kb` を実行してインデックスを更新してください。
"""


def _extract_due_date(text: str) -> str | None:
    """本文から最初の日付を抽出して 'YYYY-MM-DD' 形式で返す。見つからなければ None。"""
    import datetime
    today = datetime.date.today()
    m = _DATE_RE.search(text)
    if not m:
        return None
    y = int(m.group("y")) if m.group("y") else today.year
    mo = int(m.group("m"))
    d = int(m.group("d"))
    try:
        return datetime.date(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


class SlackBot:
    """Socket Mode で動作する KB 質問応答 Bot"""

    def __init__(self, bot_token: str, app_token: str, vector_store, gemini_client,
                 n_results: int = 5, firestore_client=None, backlog_client=None,
                 shared_info_cfg: dict = None, slack_user_to_backlog: dict = None):
        self._bot_token = bot_token
        self._app_token = app_token
        self.vs = vector_store
        self.gemini = gemini_client
        self.n_results = n_results
        self.fs = firestore_client
        self.bl = backlog_client
        # shared_info_cfg: {"project_key": "SALES_TEAM", "issue_type_id": 915353}
        self._shared_info_cfg = shared_info_cfg or {}
        # slack_user_id -> backlog_user_id のマッピング（myself + team_members から構築）
        self._slack_to_backlog: dict[str, int] = slack_user_to_backlog or {}
        self._bot_user_id: str = ""
        # thread_ts → bot が投稿した返信の ts（削除用）
        # DM は channel_id → bot が投稿した最新 ts
        self._reply_ts: dict[str, str] = {}
        # Backlog project_id キャッシュ
        self._project_id_cache: dict[str, int] = {}

    def _get_backlog_project_id(self, project_key: str) -> int | None:
        """プロジェクトキーから project_id を取得（キャッシュ付き）"""
        if project_key in self._project_id_cache:
            return self._project_id_cache[project_key]
        if not self.bl:
            return None
        try:
            project = self.bl.get_project(project_key)
            pid = project["id"]
            self._project_id_cache[project_key] = pid
            return pid
        except Exception as e:
            logger.warning(f"Backlog プロジェクト取得失敗 ({project_key}): {e}")
            return None

    def _handle_shared_info(self, raw_text: str, slack_user_id: str, channel: str) -> str:
        """共有事項コマンドを解析して Backlog に課題を起票し、KB にも保存する"""
        if not self.bl:
            return "⚠️ Backlog クライアントが設定されていないため起票できません。"
        if not self._shared_info_cfg.get("issue_type_id"):
            return "⚠️ config.yaml の `slack_bot.shared_info.issue_type_id` が未設定です。"

        # プレフィックスを除去
        text = raw_text
        for prefix in _SHARED_INFO_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].lstrip(" 　:：")
                break

        if not text:
            return (
                "共有事項の内容を入力してください。\n"
                "例: `@Wasabi Bot 共有事項 タイトル: 内容`"
            )

        # タイトルと本文を分離（「タイトル: 本文」 or 「: 本文」 or 「本文のみ」）
        # 区切りは ": " "：" ": " いずれか
        sep_m = re.search(r"[:：]\s*", text)
        if sep_m and sep_m.start() > 0:
            summary = text[:sep_m.start()].strip()
            body = text[sep_m.end():].strip()
        else:
            # タイトル省略 → 本文冒頭30文字をタイトルに
            body = text.strip()
            summary = body[:30] + ("…" if len(body) > 30 else "")

        if not body:
            body = summary

        # 期限を本文から抽出
        due_date = _extract_due_date(body)

        # Slack user → Backlog user
        assignee_id = self._slack_to_backlog.get(slack_user_id)

        # プロジェクト情報
        project_key = self._shared_info_cfg.get("project_key", "SALES_TEAM")
        issue_type_id = self._shared_info_cfg["issue_type_id"]
        project_id = self._get_backlog_project_id(project_key)
        if not project_id:
            return f"⚠️ Backlog プロジェクト `{project_key}` の取得に失敗しました。"

        # Backlog 課題説明文（投稿者・チャンネル情報を付記）
        description = (
            f"{body}\n\n"
            f"---\n"
            f"*Slack Bot（Wasabi）経由で起票*\n"
            f"投稿者 Slack ID: {slack_user_id} / チャンネル: {channel}"
        )

        try:
            issue = self.bl.create_issue(
                project_id=project_id,
                summary=summary,
                description=description,
                issue_type_id=issue_type_id,
                assignee_id=assignee_id,
                due_date=due_date,
            )
        except Exception as e:
            logger.error(f"Backlog 課題起票失敗: {e}")
            return f"⚠️ Backlog への起票に失敗しました: {e}"

        issue_key = issue.get("issueKey", "")
        base_url = self.bl.base_url
        issue_url = f"{base_url}/view/{issue_key}" if issue_key else ""

        # Firestore KB に保存
        if self.fs:
            try:
                self.fs.save_shared_info(
                    issue_key=issue_key,
                    summary=summary,
                    body=body,
                    slack_user_id=slack_user_id,
                    channel=channel,
                    due_date=due_date,
                )
            except Exception as e:
                logger.warning(f"共有事項 KB 保存失敗: {e}")

        assignee_note = f"\n・担当者: Backlog ID {assignee_id}" if assignee_id else ""
        due_note = f"\n・期限: {due_date}" if due_date else ""
        return (
            f"✅ 共有事項を Backlog に起票しました\n"
            f"・課題: {issue_url or issue_key}\n"
            f"・件名: {summary}"
            f"{assignee_note}"
            f"{due_note}"
        )

    def _handle_memo(self, raw_text: str, user: str, channel: str) -> str:
        """メモコマンドを解析して Firestore に保存し、確認メッセージを返す"""
        text = raw_text
        for prefix in _MEMO_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].lstrip(" 　:：")
                break

        issue_key = ""
        m = _ISSUE_KEY_RE.search(text)
        if m:
            issue_key = m.group(1)
            text = _ISSUE_KEY_RE.sub("", text, count=1).lstrip(" 　:：").strip()

        if not text:
            return "メモの内容を入力してください。\n例: `@Wasabi Bot メモ SALES_TEAM-23: 内容`"

        if self.fs:
            self.fs.save_manual_memo(
                text=text,
                parent_issue_key=issue_key,
                created_by=user,
                channel=channel,
            )
            issue_label = issue_key if issue_key else "未指定（週次レポート実行時に関連課題へ自動追加）"
            return (
                f"✅ 進捗メモを保存しました\n"
                f"・課題: {issue_label}\n"
                f"・内容: {text}\n"
                f"次の週次レポート実行時に情報源として使用されます。"
            )
        else:
            logger.warning(f"手動メモ: Firestore 未設定のため保存スキップ（{text}）")
            return "⚠️ Firestore が設定されていないため保存できませんでした。"

    def _search_and_answer(self, query: str) -> str:
        """クエリを KB 検索 → Gemini で回答生成"""
        if self.vs.count() == 0:
            return (
                "KB にデータがありません。\n"
                "`python main.py --only kb` を実行してインデックスを構築してください。"
            )
        results = self.vs.search(query, n_results=self.n_results)
        if not results:
            return "KB に該当情報が見つかりませんでした。"
        answer = self.gemini.answer_with_context(query, results)
        return answer or "回答の生成に失敗しました。"

    def _dispatch(self, text: str, user: str, channel: str, say_fn, thread_ts: str = None):
        """コマンド判定と処理の共通ロジック。thread_ts=None は DM。"""
        kwargs = {"thread_ts": thread_ts} if thread_ts else {}

        # 削除コマンド
        if text.lower() in _DELETE_COMMANDS:
            key = thread_ts if thread_ts else channel
            bot_ts = self._reply_ts.pop(key, None)
            if bot_ts:
                # say_fn の client は外側スコープから渡せないため、削除は呼び出し元で処理
                return "_delete_", bot_ts
            say_fn(
                text="削除対象の Bot 返信が見つかりませんでした。\n"
                     "Bot 再起動後は返信の記録がリセットされます。",
                **kwargs,
            )
            return None, None

        # ヘルプ
        if not text or text in ("help", "ヘルプ", "?", "？"):
            say_fn(text=_HELP_TEXT, **kwargs)
            return None, None

        # 共有事項コマンド
        if any(text.lower().startswith(p.lower()) for p in _SHARED_INFO_PREFIXES):
            reply = self._handle_shared_info(text, user, channel)
            say_fn(text=reply, **kwargs)
            return None, None

        # メモコマンド
        if any(text.lower().startswith(p.lower()) for p in _MEMO_PREFIXES):
            reply = self._handle_memo(text, user, channel)
            say_fn(text=reply, **kwargs)
            return None, None

        # KB 質問（RAG）
        logger.info(f"Slack Bot 質問受信: {text}")
        answer = self._search_and_answer(text)
        resp = say_fn(text=answer, **kwargs)
        if resp and resp.get("ts"):
            store_key = thread_ts if thread_ts else channel
            self._reply_ts[store_key] = resp["ts"]
        return None, None

    def run(self):
        """Socket Mode で Bot を起動（ブロッキング）"""
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError:
            logger.error(
                "slack-bolt が未インストールです。`pip install slack-bolt` を実行してください。"
            )
            return

        app = App(token=self._bot_token)

        try:
            self._bot_user_id = app.client.auth_test()["user_id"]
            logger.info(f"Slack Bot user_id: {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Bot user_id 取得失敗: {e}")

        @app.event("app_mention")
        def handle_mention(event, client, say):
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
            thread_ts = event.get("thread_ts") or event.get("ts")
            user = event.get("user", "")
            channel = event["channel"]

            cmd, bot_ts = self._dispatch(text, user, channel, say, thread_ts=event.get("ts"))
            if cmd == "_delete_":
                # thread_ts がある場合はスレッドの根 ts がキー
                key = event.get("thread_ts")
                if key:
                    actual_ts = self._reply_ts.pop(key, bot_ts)
                else:
                    actual_ts = bot_ts
                try:
                    client.chat_delete(channel=channel, ts=actual_ts)
                    logger.info(f"Bot メッセージ削除: ts={actual_ts}")
                except Exception as e:
                    logger.warning(f"chat_delete 失敗: {e}")
                    say(text=f"削除に失敗しました: {e}", thread_ts=event.get("ts"))

        @app.event("message")
        def handle_dm(event, client, say):
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()
            if not text:
                return

            user = event.get("user", "")
            channel = event["channel"]

            cmd, bot_ts = self._dispatch(text, user, channel, say, thread_ts=None)
            if cmd == "_delete_":
                try:
                    client.chat_delete(channel=channel, ts=bot_ts)
                    logger.info(f"DM Bot メッセージ削除: ts={bot_ts}")
                except Exception as e:
                    logger.warning(f"chat_delete 失敗: {e}")
                    say(f"削除に失敗しました: {e}")

        logger.info("Slack Bot 起動中... (Socket Mode)")
        logger.info("チャンネル内でメンション、またはDMで質問できます。Ctrl+C で停止。")
        SocketModeHandler(app, self._app_token).start()
