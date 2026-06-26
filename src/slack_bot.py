"""
Slack Bot - KB への自然言語質問に答えるインタラクティブ Bot
Socket Mode で動作するため、パブリック URL 不要。

起動: python main.py --bot
対応イベント:
  - app_mention: チャンネル内でメンションされた質問
  - message (im): DM で送られた質問
"""
import logging
import re

logger = logging.getLogger(__name__)

_HELP_TEXT = """\
*Weekly Relay KB アシスタント* にようこそ！
チケット・Slack・議事録の KB を検索して回答します。

*使い方（チャンネル内）*
　`@Weekly Relay ACE刷新の進捗は？`

*使い方（DM）*
　`ポスタス催事店の課題は？`

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

        @app.event("app_mention")
        def handle_mention(event, say):
            # メンション部分（<@UXXXX>）を除去して質問テキストを取得
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
            if not text or text in ("help", "ヘルプ", "?", "？"):
                say(_HELP_TEXT)
                return
            logger.info(f"Slack Bot メンション受信: {text}")
            answer = self._search_and_answer(text)
            # スレッドに返信
            say(text=answer, thread_ts=event.get("ts"))

        @app.event("message")
        def handle_dm(event, say):
            # DM 以外・Bot 自身のメッセージ・サブタイプ付き（bot_message 等）は無視
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()
            if not text:
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
