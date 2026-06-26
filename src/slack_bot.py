"""
Slack Bot - KB への自然言語質問に答えるインタラクティブ Bot
Socket Mode で動作するため、パブリック URL 不要。

起動: python main.py --bot
対応イベント:
  - app_mention: チャンネル内でメンションされた質問
  - message (im): DM で送られた質問

削除コマンド:
  チャンネル: Bot の返信スレッド内で「@Weekly Relay delete」と送信
  DM        : 「delete」とだけ送信
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

*Bot の返信を削除したいとき（チャンネル）*
　返信スレッド内で `@Weekly Relay delete` と送信

*使い方（DM）*
　質問をそのまま送信。削除は `delete` と送信。

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
        self._bot_user_id: str = ""
        # thread_ts → bot が投稿した返信の ts（削除用）
        # DM は channel_id → bot が投稿した最新 ts
        self._reply_ts: dict[str, str] = {}

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

        # 起動時に Bot 自身の user ID を取得
        try:
            self._bot_user_id = app.client.auth_test()["user_id"]
            logger.info(f"Slack Bot user_id: {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Bot user_id 取得失敗: {e}")

        @app.event("app_mention")
        def handle_mention(event, client, say):
            # メンション部分（<@UXXXX>）を除去して質問テキストを取得
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()

            # 削除コマンド
            if text.lower() in _DELETE_COMMANDS:
                # スレッド内メンションの場合: thread_ts がそのスレッドの根
                thread_ts = event.get("thread_ts")
                if thread_ts and thread_ts in self._reply_ts:
                    bot_ts = self._reply_ts.pop(thread_ts)
                    try:
                        client.chat_delete(channel=event["channel"], ts=bot_ts)
                        logger.info(f"Bot メッセージ削除: ts={bot_ts}")
                    except Exception as e:
                        logger.warning(f"chat_delete 失敗: {e}")
                        say(text=f"削除に失敗しました: {e}", thread_ts=thread_ts)
                else:
                    say(
                        text="削除対象の Bot 返信が見つかりませんでした。\n"
                             "Bot 再起動後は返信の記録がリセットされます。",
                        thread_ts=event.get("ts"),
                    )
                return

            if not text or text in ("help", "ヘルプ", "?", "？"):
                say(text=_HELP_TEXT, thread_ts=event.get("ts"))
                return

            logger.info(f"Slack Bot メンション受信: {text}")
            answer = self._search_and_answer(text)
            # スレッドに返信し、ts を記録
            resp = say(text=answer, thread_ts=event.get("ts"))
            if resp and resp.get("ts"):
                self._reply_ts[event["ts"]] = resp["ts"]

        @app.event("message")
        def handle_dm(event, client, say):
            # DM 以外・Bot 自身のメッセージ・サブタイプ付きは無視
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()
            if not text:
                return

            # 削除コマンド（DM）
            if text.lower() in _DELETE_COMMANDS:
                channel = event["channel"]
                bot_ts = self._reply_ts.pop(channel, None)
                if bot_ts:
                    try:
                        client.chat_delete(channel=channel, ts=bot_ts)
                        logger.info(f"DM Bot メッセージ削除: ts={bot_ts}")
                    except Exception as e:
                        logger.warning(f"chat_delete 失敗: {e}")
                        say(f"削除に失敗しました: {e}")
                else:
                    say("削除対象の Bot 返信が見つかりませんでした。\n"
                        "Bot 再起動後は返信の記録がリセットされます。")
                return

            if text in ("help", "ヘルプ", "?", "？"):
                say(_HELP_TEXT)
                return

            logger.info(f"Slack Bot DM受信: {text}")
            answer = self._search_and_answer(text)
            resp = say(answer)
            # DM の削除用に最新の bot 返信 ts を channel をキーに保存
            if resp and resp.get("ts"):
                self._reply_ts[event["channel"]] = resp["ts"]

        logger.info("Slack Bot 起動中... (Socket Mode)")
        logger.info("チャンネル内でメンション、またはDMで質問できます。Ctrl+C で停止。")
        SocketModeHandler(app, self._app_token).start()
