"""
Slack Bot - KB への自然言語質問に答えるインタラクティブ Bot
Socket Mode で動作するため、パブリック URL 不要。

起動: python main.py --bot
対応イベント:
  - app_mention: チャンネル内でメンションされた質問
  - message (im): DM で送られた質問

削除コマンド:
  スレッド内で「delete」「削除」と送ると、そのスレッドの Bot 返信を削除する
  DM で「delete」「削除」と送ると、直前の Bot 返信を削除する
"""
import logging
import re

logger = logging.getLogger(__name__)

_DELETE_COMMANDS = {"delete", "削除", "del", "消して", "消去"}

_HELP_TEXT = """\
*Weekly Relay KB アシスタント* にようこそ！
チケット・Slack・議事録の KB を検索して回答します。

*使い方（チャンネル内）*
　`@Weekly Relay ACE刷新の進捗は？`

*使い方（DM）*
　`ポスタス催事店の課題は？`

*Bot の返信を削除したいとき*
　返信スレッド内で `delete` または `削除` と送信

KB が空の場合は `python main.py --only kb` を実行してインデックスを更新してください。
"""


class SlackBot:
    """Socket Mode で動作する KB 質問応答 Bot"""

    def __init__(self, bot_token: str, app_token: str, vector_store, gemini_client,
                 n_results: int = 5):
        self._bot_token = bot_token
        self._app_token = app_token
        self.vs = vector_store
        self.gemini = gemini_client
        self.n_results = n_results
        self._bot_user_id: str = ""  # auth.test() で起動時に取得

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

    def _delete_bot_messages_in_thread(self, client, channel: str, thread_ts: str) -> int:
        """指定スレッド内の Bot メッセージをすべて削除して削除件数を返す"""
        try:
            result = client.conversations_replies(channel=channel, ts=thread_ts)
            deleted = 0
            for msg in result.get("messages", []):
                if msg.get("user") == self._bot_user_id:
                    client.chat_delete(channel=channel, ts=msg["ts"])
                    deleted += 1
            return deleted
        except Exception as e:
            logger.warning(f"スレッド内 Bot メッセージ削除失敗: {e}")
            return 0

    def _delete_last_bot_message_in_dm(self, client, channel: str) -> int:
        """DM チャンネルの直近 Bot メッセージ1件を削除して削除件数を返す"""
        try:
            result = client.conversations_history(channel=channel, limit=20)
            for msg in result.get("messages", []):
                if msg.get("user") == self._bot_user_id:
                    client.chat_delete(channel=channel, ts=msg["ts"])
                    return 1
            return 0
        except Exception as e:
            logger.warning(f"DM Bot メッセージ削除失敗: {e}")
            return 0

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

        # 起動時に Bot 自身の user ID を取得（削除判定に使用）
        try:
            self._bot_user_id = app.client.auth_test()["user_id"]
            logger.info(f"Slack Bot user_id: {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Bot user_id 取得失敗: {e}")

        @app.event("app_mention")
        def handle_mention(event, client, say):
            # メンション部分（<@UXXXX>）を除去して質問テキストを取得
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()

            # 削除コマンド: スレッド内で「delete」等を送ると Bot 返信を削除
            if text.lower() in _DELETE_COMMANDS:
                thread_ts = event.get("thread_ts") or event.get("ts")
                deleted = self._delete_bot_messages_in_thread(
                    client, event["channel"], thread_ts
                )
                if deleted:
                    logger.info(f"Bot メッセージ削除: {deleted} 件（thread_ts={thread_ts}）")
                else:
                    say(text="削除対象の Bot メッセージが見つかりませんでした。",
                        thread_ts=event.get("ts"))
                return

            if not text or text in ("help", "ヘルプ", "?", "？"):
                say(text=_HELP_TEXT, thread_ts=event.get("ts"))
                return

            logger.info(f"Slack Bot メンション受信: {text}")
            answer = self._search_and_answer(text)
            # スレッドに返信
            say(text=answer, thread_ts=event.get("ts"))

        @app.event("message")
        def handle_dm(event, client, say):
            # DM 以外・Bot 自身のメッセージ・サブタイプ付き（bot_message 等）は無視
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()
            if not text:
                return

            # 削除コマンド: DM で「delete」等を送ると直前の Bot 返信を削除
            if text.lower() in _DELETE_COMMANDS:
                deleted = self._delete_last_bot_message_in_dm(client, event["channel"])
                if deleted:
                    logger.info("DM Bot メッセージ削除: 1 件")
                else:
                    say("削除対象の Bot メッセージが見つかりませんでした。")
                return

            if text in ("help", "ヘルプ", "?", "？"):
                say(_HELP_TEXT)
                return

            logger.info(f"Slack Bot DM受信: {text}")
            answer = self._search_and_answer(text)
            say(answer)

        logger.info("Slack Bot 起動中... (Socket Mode)")
        logger.info("チャンネル内でメンション、またはDMで質問できます。Ctrl+C で停止。")
        SocketModeHandler(app, self._app_token).start()
