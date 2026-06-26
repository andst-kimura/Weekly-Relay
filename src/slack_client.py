"""
Slack API クライアント
自分が送信したメッセージを全参加チャンネルから取得する
"""
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime
import logging
import time

logger = logging.getLogger(__name__)


class SlackClient:
    def __init__(self, bot_token: str, my_user_id: str):
        self.client = WebClient(token=bot_token)
        self.my_user_id = my_user_id
        self._user_cache: dict = {}
        self._user_cache_loaded = False

    def _load_user_cache(self) -> None:
        """users.list で全ユーザーを一括取得してキャッシュに格納"""
        if self._user_cache_loaded:
            return
        try:
            cursor = None
            while True:
                kwargs = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self.client.users_list(**kwargs)
                for user in response.get("members", []):
                    uid = user.get("id", "")
                    name = (
                        user.get("profile", {}).get("display_name")
                        or user.get("profile", {}).get("real_name")
                        or user.get("name", uid)
                    )
                    self._user_cache[uid] = name
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            logger.debug(f"ユーザーキャッシュ構築完了: {len(self._user_cache)}人")
        except Exception as e:
            logger.warning(f"ユーザー一覧の一括取得に失敗（個別取得にフォールバック）: {e}")
        finally:
            self._user_cache_loaded = True

    def get_user_name(self, user_id: str) -> str:
        """ユーザーIDを表示名に変換（一括キャッシュ利用）"""
        self._load_user_cache()
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        # キャッシュにない場合のみ個別取得
        try:
            response = self.client.users_info(user=user_id)
            user = response["user"]
            name = (
                user.get("profile", {}).get("display_name")
                or user.get("profile", {}).get("real_name")
                or user.get("name", user_id)
            )
            self._user_cache[user_id] = name
            return name
        except Exception:
            self._user_cache[user_id] = user_id
            return user_id

    def resolve_user_mentions(self, text: str) -> str:
        """テキスト内の<@UXXXXXXX>を実名に置換"""
        import re
        mentions = re.findall(r"<@(U[A-Z0-9]+)>", text)
        for uid in set(mentions):
            name = self.get_user_name(uid)
            text = text.replace(f"<@{uid}>", f"@{name}")
        return text

    def get_my_channels(self) -> list[dict]:
        """自分が参加している全チャンネルを取得"""
        channels = []
        cursor = None

        while True:
            try:
                kwargs = {
                    "types": "public_channel,private_channel",
                    "exclude_archived": True,
                    "limit": 200,
                }
                if cursor:
                    kwargs["cursor"] = cursor

                response = self.client.conversations_list(**kwargs)
                for channel in response["channels"]:
                    if channel.get("is_member", False):
                        channels.append(channel)

                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

                time.sleep(1)

            except SlackApiError as e:
                if "ratelimited" in str(e):
                    logger.warning("Slackレート制限に達しました。30秒待機して再試行します...")
                    time.sleep(30)
                    continue
                logger.error(f"チャンネル一覧取得エラー: {e}")
                break

        logger.info(f"参加チャンネル数: {len(channels)}")
        return channels

    def get_my_messages_in_channel(self, channel_id: str, channel_name: str,
                                    since: datetime, until: datetime) -> list[dict]:
        """指定チャンネルから自分のメッセージを取得"""
        my_messages = []

        try:
            oldest = str(since.timestamp())
            latest = str(until.timestamp())

            response = self.client.conversations_history(
                channel=channel_id,
                oldest=oldest,
                latest=latest,
                limit=1000,
            )
            messages = response.get("messages", [])

            for msg in messages:
                if msg.get("user") == self.my_user_id and msg.get("type") == "message":
                    # サブタイプがあるもの（参加通知等）はスキップ
                    if msg.get("subtype"):
                        continue
                    ts = float(msg.get("ts", 0))
                    raw_text = msg.get("text", "")
                    resolved_text = self.resolve_user_mentions(raw_text)
                    my_messages.append({
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "text": resolved_text,
                        "ts": ts,
                        "datetime": datetime.fromtimestamp(ts),
                        "thread_ts": msg.get("thread_ts"),
                        "is_thread_reply": msg.get("thread_ts") is not None and msg.get("thread_ts") != msg.get("ts"),
                        "reply_count": msg.get("reply_count", 0),
                    })

            # スレッド返信も取得（自分が親投稿者 or メンションされているスレッドのみ）
            for msg in messages:
                if msg.get("reply_count", 0) > 0:
                    is_my_thread = msg.get("user") == self.my_user_id
                    is_mentioned = f"<@{self.my_user_id}>" in msg.get("text", "")
                    if is_my_thread or is_mentioned:
                        thread_replies = self._get_my_thread_replies(
                            channel_id, channel_name, msg["ts"], since, until
                        )
                        my_messages.extend(thread_replies)

        except SlackApiError as e:
            if "not_in_channel" in str(e):
                logger.debug(f"チャンネル '{channel_name}' は参加していません（スキップ）")
            else:
                logger.warning(f"チャンネル '{channel_name}' の取得エラー: {e}")

        return my_messages

    def _get_my_thread_replies(self, channel_id: str, channel_name: str,
                                thread_ts: str, since: datetime, until: datetime) -> list[dict]:
        """スレッドの返信から自分のメッセージを取得"""
        my_replies = []
        try:
            response = self.client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=200,
            )   
            for msg in response.get("messages", [])[1:]:  # 最初のメッセージ（親）はスキップ
                if msg.get("user") == self.my_user_id:
                    ts = float(msg.get("ts", 0))
                    msg_time = datetime.fromtimestamp(ts)
                    if since <= msg_time <= until:
                        raw_text = msg.get("text", "")
                        resolved_text = self.resolve_user_mentions(raw_text)
                        my_replies.append({
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "text": resolved_text,
                            "ts": ts,
                            "datetime": msg_time,
                            "thread_ts": thread_ts,
                            "is_thread_reply": True,
                            "reply_count": 0,
                        })
        except SlackApiError as e:
            logger.debug(f"スレッド取得エラー: {e}")
        return my_replies

    def get_full_thread(self, channel_id: str, thread_ts: str) -> list[dict]:
        """スレッド全体を取得（他者の発言含む）"""
        try:
            response = self.client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=200,
            )
            messages = []
            for msg in response.get("messages", []):
                ts = float(msg.get("ts", 0))
                raw_text = msg.get("text", "")
                resolved_text = self.resolve_user_mentions(raw_text)
                user_id = msg.get("user", "")
                messages.append({
                    "user_id": user_id,
                    "user_name": self.get_user_name(user_id),
                    "text": resolved_text,
                    "ts": ts,
                    "datetime": datetime.fromtimestamp(ts),
                    "is_mine": user_id == self.my_user_id,
                })
            return messages
        except SlackApiError as e:
            logger.warning(f"スレッド全体取得エラー: {e}")
            return []

    def send_dm(self, text: str) -> bool:
        """自分宛にSlack DMを送信"""
        try:
            self.client.chat_postMessage(channel=self.my_user_id, text=text)
            logger.info("Slack DM 送信完了")
            return True
        except SlackApiError as e:
            logger.error(f"Slack DM 送信失敗: {e}")
            return False

    def get_team_messages_in_channel(self, channel_id: str, channel_name: str,
                                      since: datetime, until: datetime,
                                      team_user_ids: list[str]) -> list[dict]:
        """チームメンバー全員のトップレベル投稿を取得する。
        スレッド内の返信は knowledge_base 側で get_full_thread により補完される。"""
        team_set = set(team_user_ids)
        messages = []
        try:
            response = self.client.conversations_history(
                channel=channel_id,
                oldest=str(since.timestamp()),
                latest=str(until.timestamp()),
                limit=1000,
            )
            for msg in response.get("messages", []):
                if msg.get("subtype") or msg.get("type") != "message":
                    continue
                uid = msg.get("user", "")
                if uid not in team_set:
                    continue
                ts = float(msg.get("ts", 0))
                resolved_text = self.resolve_user_mentions(msg.get("text", ""))
                messages.append({
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "user_id": uid,
                    "user_name": self.get_user_name(uid),
                    "text": resolved_text,
                    "ts": ts,
                    "datetime": datetime.fromtimestamp(ts),
                    "thread_ts": msg.get("thread_ts"),
                    "is_mine": uid == self.my_user_id,
                    "reply_count": msg.get("reply_count", 0),
                })
        except SlackApiError as e:
            if "not_in_channel" in str(e):
                logger.debug(f"チャンネル '{channel_name}' は参加していません（スキップ）")
            else:
                logger.warning(f"チャンネル '{channel_name}' の取得エラー: {e}")
        return messages

    def get_all_my_messages(self, since: datetime, until: datetime) -> list[dict]:
        """全参加チャンネルから自分のメッセージを取得"""
        channels = self.get_my_channels()
        all_messages = []

        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel.get("name", channel_id)
            logger.info(f"Slack: #{channel_name} を処理中...")

            messages = self.get_my_messages_in_channel(channel_id, channel_name, since, until)
            all_messages.extend(messages)

        logger.info(f"合計メッセージ数: {len(all_messages)}")
        return all_messages
