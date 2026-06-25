"""
ナレッジベース生成モジュール
チケット / Slack / 議事録を Firestore の context_snapshots コレクションへ蓄積する。

チケット KB は ThreadPoolExecutor で並列フェッチし、
Firestore に保存済みの backlog_updated_at と比較して変化のないチケットをスキップする。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging

from src.backlog_client import BacklogClient
from src.slack_client import SlackClient

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_TICKET_WORKERS = 5   # Backlog API の並列数
_MEETING_WORKERS = 5  # 議事録 KB（ドキュメント取得済みのため並列安全）
_SLACK_WORKERS = 3    # Slack KB（API 呼び出しあり、レート制限を考慮して控えめ）


class KnowledgeBase:
    def __init__(self, backlog_client: BacklogClient, slack_client: SlackClient,
                 output_dir: str = "output/knowledge",  # 後方互換のため残す（未使用）
                 gemini_client=None, firestore_client=None):
        self.backlog = backlog_client
        self.slack = slack_client
        self.gemini = gemini_client
        self.fs = firestore_client  # None の場合はログ警告のみ

    def generate(self, activities: list[dict], since: datetime, until: datetime,
                 meeting_docs: list[dict] = None) -> None:
        """週次KB生成のエントリーポイント（run_weekly_report から呼ぶ）"""
        logger.info("ナレッジベース生成開始")
        self._generate_ticket_knowledge(activities)
        self._generate_slack_knowledge(since, until)
        if meeting_docs:
            self._generate_meeting_knowledge(meeting_docs)
        logger.info("ナレッジベース生成完了")

    # ------------------------------------------------------------------ #
    #  チケット別ナレッジ
    # ------------------------------------------------------------------ #

    def _generate_ticket_knowledge(self, activities: list[dict]) -> None:
        # issue_key で重複排除
        issues_seen: dict[str, dict] = {}
        for act in activities:
            key = act.get("issue_key", "")
            if key and key not in issues_seen:
                issues_seen[key] = act

        if not issues_seen:
            return

        logger.info(f"チケットKB: {len(issues_seen)} 件を並列処理（workers={_TICKET_WORKERS}）")

        with ThreadPoolExecutor(max_workers=_TICKET_WORKERS) as executor:
            futures = {
                executor.submit(self._process_ticket, issue_key, act): issue_key
                for issue_key, act in issues_seen.items()
            }
            for future in as_completed(futures):
                issue_key = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"チケットKB生成失敗 {issue_key}: {e}")

    def _process_ticket(self, issue_key: str, act: dict) -> None:
        """1チケットの処理（差分スキップ → Backlog API → Firestore 保存）"""
        backlog_updated_at = (act.get("updated") or "")[:19]  # "YYYY-MM-DDTHH:MM:SS"

        # ② 差分スキップ: Firestoreの保存済み updated_at と比較
        if self.fs and backlog_updated_at:
            doc_id = f"ticket_{issue_key}"
            existing = self.fs.get_context_snapshot(doc_id)
            if existing and existing.get("backlog_updated_at") == backlog_updated_at:
                logger.info(f"チケットKBスキップ（変更なし）: {issue_key}")
                return

        # ① Backlog API フェッチ（並列実行される）
        issue = self.backlog.get_issue(issue_key)
        comments = self.backlog.get_all_comments(issue["id"])

        assignee = issue.get("assignee") or {}
        status_name = issue.get("status", {}).get("name", "")
        summary = issue.get("summary", "")
        description = issue.get("description") or ""

        history_lines = [
            f"[{self._format_datetime(c.get('created', ''))}] "
            f"{(c.get('createdUser') or {}).get('name', '不明')}: "
            f"{(c.get('content') or '')[:300]}"
            for c in comments
        ]

        ai_summary = ""
        if self.gemini and self.gemini.enabled and comments:
            ai_summary = self.gemini.summarize_ticket(
                summary, status_name, "\n".join(history_lines)
            ) or ""

        data = {
            "source_type": "ticket",
            "issue_key": issue_key,
            "project_name": act.get("project_name", ""),
            "summary": summary,
            "status": status_name,
            "assignee": assignee.get("name", "未設定"),
            "description": description,
            "backlog_updated_at": backlog_updated_at,
            "ai_summary": ai_summary,
            "comment_count": len(comments),
            "comments": [
                {
                    "created": (c.get("created") or "")[:19],
                    "user": (c.get("createdUser") or {}).get("name", "不明"),
                    "content": (c.get("content") or "")[:500],
                }
                for c in comments
            ],
        }

        if self.fs:
            self.fs.save_context_snapshot(f"ticket_{issue_key}", data)
            logger.info(f"チケットKB保存: ticket_{issue_key}")
        else:
            logger.warning(f"FirestoreClient 未設定のためチケットKBをスキップ: {issue_key}")

    # ------------------------------------------------------------------ #
    #  Slack チャンネル別ナレッジ
    # ------------------------------------------------------------------ #

    def _generate_slack_knowledge(self, since: datetime, until: datetime) -> None:
        week_num = since.strftime("%Y%W")
        iso_week = since.isocalendar()[1]
        week_label = f"{since.strftime('%Y年')}第{iso_week}週"

        channels = self.slack.get_my_channels()

        def _save(channel):
            self._save_slack_channel(
                channel["id"], channel.get("name", channel["id"]),
                since, until, week_num, week_label,
            )

        with ThreadPoolExecutor(max_workers=_SLACK_WORKERS) as executor:
            futures = {executor.submit(_save, ch): ch.get("name", ch["id"]) for ch in channels}
            for future in as_completed(futures):
                ch_name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"Slack KB生成失敗 #{ch_name}: {e}")

    def _save_slack_channel(self, channel_id: str, channel_name: str,
                             since: datetime, until: datetime,
                             week_num: str, week_label: str) -> None:
        my_messages = self.slack.get_my_messages_in_channel(channel_id, channel_name, since, until)
        if not my_messages:
            return

        # 参加したスレッドの thread_ts を収集
        thread_tss: set[str] = set()
        standalone: list[dict] = []
        for msg in my_messages:
            ts = msg.get("thread_ts")
            if ts:
                thread_tss.add(ts)
            else:
                standalone.append(msg)

        threads: dict[str, list[dict]] = {}
        for ts in thread_tss:
            full = self.slack.get_full_thread(channel_id, ts)
            if full:
                threads[ts] = full

        # Gemini 要約
        ai_summary = ""
        if self.gemini and self.gemini.enabled:
            all_msgs_text = []
            for ts, msgs in sorted(threads.items(), key=lambda x: float(x[0])):
                for msg in msgs:
                    user = msg.get("user_name") or "不明"
                    t = msg["datetime"].strftime("%m/%d %H:%M")
                    mark = "★" if msg.get("is_mine") else ""
                    all_msgs_text.append(f"[{t}] {mark}{user}: {(msg.get('text') or '')[:200]}")
            for msg in standalone:
                t = msg["datetime"].strftime("%m/%d %H:%M")
                all_msgs_text.append(f"[{t}] ★自分: {(msg.get('text') or '')[:200]}")
            if all_msgs_text:
                period = f"{since.strftime('%Y/%m/%d')} 〜 {until.strftime('%Y/%m/%d')}"
                ai_summary = self.gemini.summarize_slack_channel(
                    channel_name, period, "\n".join(all_msgs_text)
                ) or ""

        doc_id = f"slack_{week_num}_{channel_name}"
        data = {
            "source_type": "slack",
            "channel_id": channel_id,
            "channel_name": channel_name,
            "week_num": week_num,
            "week_label": week_label,
            "period_start": since.isoformat(),
            "period_end": until.isoformat(),
            "ai_summary": ai_summary,
            "standalone_messages": [
                {
                    "datetime": m["datetime"].isoformat(),
                    "text": (m.get("text") or "")[:500],
                }
                for m in sorted(standalone, key=lambda x: x["datetime"])
            ],
            "threads": [
                {
                    "thread_ts": ts,
                    "messages": [
                        {
                            "user": msg.get("user_name") or "不明",
                            "datetime": msg["datetime"].isoformat(),
                            "text": (msg.get("text") or "")[:500],
                            "is_mine": msg.get("is_mine", False),
                        }
                        for msg in msgs
                    ],
                }
                for ts, msgs in sorted(threads.items(), key=lambda x: float(x[0]))
            ],
        }

        if self.fs:
            self.fs.save_context_snapshot(doc_id, data)
            logger.info(f"Slack KB保存: {doc_id}")
        else:
            logger.warning(f"FirestoreClient 未設定のためSlack KBをスキップ: {doc_id}")

    # ------------------------------------------------------------------ #
    #  議事録ナレッジ
    # ------------------------------------------------------------------ #

    def _generate_meeting_knowledge(self, meeting_docs: list[dict]) -> None:
        with ThreadPoolExecutor(max_workers=_MEETING_WORKERS) as executor:
            futures = {executor.submit(self._save_meeting, doc): doc.get("title", "") for doc in meeting_docs}
            for future in as_completed(futures):
                title = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"議事録KB生成失敗 ({title}): {e}")

    def _save_meeting(self, doc: dict) -> None:
        date_str = doc["created_date"].strftime("%Y%m%d")
        title = doc["title"]
        event_summary = doc.get("event_summary", "")

        # カレンダー添付で汎用タイトルの場合はイベント名を優先
        if event_summary and ("Gemini によるメモ" in title or title.strip() == "メモ"):
            display_name = event_summary
        else:
            display_name = title

        raw_text = doc.get("text", "")
        ai_summary = ""
        if self.gemini and self.gemini.enabled and raw_text:
            ai_summary = self.gemini.summarize_meeting(raw_text) or ""

        doc_id = f"meeting_{date_str}_{(doc.get('id') or display_name)[-12:]}"
        data = {
            "source_type": "meeting",
            "title": title,
            "display_name": display_name,
            "event_summary": event_summary,
            "created_date": doc["created_date"].isoformat(),
            "raw_text": raw_text,
            "ai_summary": ai_summary,
        }

        if self.fs:
            self.fs.save_context_snapshot(doc_id, data)
            logger.info(f"議事録KB保存: {doc_id}")
        else:
            logger.warning(f"FirestoreClient 未設定のため議事録KBをスキップ: {doc_id}")

    # ------------------------------------------------------------------ #
    #  ユーティリティ
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_datetime(iso_str: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(JST)
            return dt.strftime("%Y/%m/%d %H:%M")
        except Exception:
            return iso_str
