"""
ナレッジベース生成モジュール
週次レポートと同時に実行し、チケット単位・Slackチャンネル単位でMarkdownを蓄積する
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging

from src.backlog_client import BacklogClient
from src.slack_client import SlackClient

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class KnowledgeBase:
    def __init__(self, backlog_client: BacklogClient, slack_client: SlackClient,
                 output_dir: str = "output/knowledge", gemini_client=None):
        self.backlog = backlog_client
        self.slack = slack_client
        self.gemini = gemini_client
        self.tickets_dir = Path(output_dir) / "tickets"
        self.slack_dir = Path(output_dir) / "slack"
        self.meetings_dir = Path(output_dir) / "meetings"

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
        self.tickets_dir.mkdir(parents=True, exist_ok=True)

        # issue_key で重複排除（最初に出てきたものを代表として使用）
        issues_seen: dict[str, dict] = {}
        for act in activities:
            key = act.get("issue_key", "")
            if key and key not in issues_seen:
                issues_seen[key] = act

        for issue_key, act in issues_seen.items():
            try:
                self._save_ticket_file(issue_key, act)
            except Exception as e:
                logger.warning(f"チケットKB生成失敗 {issue_key}: {e}")

    def _save_ticket_file(self, issue_key: str, act: dict) -> None:
        issue = self.backlog.get_issue(issue_key)
        comments = self.backlog.get_all_comments(issue["id"])

        assignee = issue.get("assignee") or {}
        status_name = issue.get("status", {}).get("name", "")
        summary = issue.get("summary", "")

        lines = [
            f"# {issue_key}: {summary}",
            "",
            f"**プロジェクト:** {act['project_name']}",
            f"**ステータス:** {status_name}",
            f"**担当者:** {assignee.get('name', '未設定')}",
            f"**最終更新:** {(act.get('updated') or '')[:10]}",
            "",
            "---",
            "",
        ]

        desc = issue.get("description") or ""
        if desc:
            lines += ["## 概要", "", desc, "", "---", ""]

        # Gemini による対応履歴の要約
        if self.gemini and self.gemini.enabled and comments:
            history_text = "\n".join(
                f"[{self._format_datetime(c.get('created', ''))}] "
                f"{(c.get('createdUser') or {}).get('name', '不明')}: "
                f"{(c.get('content') or '')[:300]}"
                for c in comments
            )
            ai_summary = self.gemini.summarize_ticket(summary, status_name, history_text)
            if ai_summary:
                lines += ["## Weekly Relay 要約", "", ai_summary, "", "---", ""]

        if comments:
            lines += ["## 対応履歴（原文）", ""]
            for c in comments:
                time_str = self._format_datetime(c.get("created", ""))
                user_name = (c.get("createdUser") or {}).get("name", "不明")
                content = c.get("content") or ""
                lines += [f"### {time_str} - {user_name}", "", content, ""]

        filename = self.tickets_dir / f"{issue_key}.md"
        filename.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"チケットKB保存: {filename.name}")

    # ------------------------------------------------------------------ #
    #  Slack チャンネル別ナレッジ
    # ------------------------------------------------------------------ #

    def _generate_slack_knowledge(self, since: datetime, until: datetime) -> None:
        self.slack_dir.mkdir(parents=True, exist_ok=True)
        week_num = since.strftime("%Y%W")
        iso_week = since.isocalendar()[1]
        week_label = f"{since.strftime('%Y年')}第{iso_week}週"

        channels = self.slack.get_my_channels()
        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel.get("name", channel_id)
            try:
                self._save_slack_channel_file(
                    channel_id, channel_name, since, until, week_num, week_label
                )
            except Exception as e:
                logger.warning(f"Slack KB生成失敗 #{channel_name}: {e}")

    def _save_slack_channel_file(self, channel_id: str, channel_name: str,
                                  since: datetime, until: datetime,
                                  week_num: str, week_label: str) -> None:
        my_messages = self.slack.get_my_messages_in_channel(channel_id, channel_name, since, until)
        if not my_messages:
            return  # 発言なしのチャンネルはスキップ

        # 参加したスレッドの thread_ts を収集
        thread_tss: set[str] = set()
        standalone: list[dict] = []
        for msg in my_messages:
            ts = msg.get("thread_ts")
            if ts:
                thread_tss.add(ts)
            else:
                standalone.append(msg)

        # スレッド全体を取得（他者の発言含む）
        threads: dict[str, list[dict]] = {}
        for ts in thread_tss:
            full = self.slack.get_full_thread(channel_id, ts)
            if full:
                threads[ts] = full

        lines = [
            f"# #{channel_name} 週次記録（{week_label}）",
            f"**期間:** {since.strftime('%Y/%m/%d')} 〜 {until.strftime('%Y/%m/%d')}",
            "",
            "---",
            "",
        ]

        # Gemini による発言まとめ
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
                )
                if ai_summary:
                    lines += ["## Weekly Relay 要約", "", ai_summary, "", "---", ""]

        if threads:
            lines += ["## スレッド（原文）", ""]
            for ts, msgs in sorted(threads.items(), key=lambda x: float(x[0])):
                first = msgs[0] if msgs else {}
                time_str = first.get("datetime", datetime.fromtimestamp(float(ts))).strftime("%Y/%m/%d %H:%M")
                lines += [f"### {time_str}", ""]
                for msg in msgs:
                    user = msg.get("user_name") or "不明"
                    t = msg["datetime"].strftime("%H:%M")
                    mark = " ★" if msg.get("is_mine") else ""
                    lines += [f"**{user}**{mark} `{t}`", msg.get("text") or "", ""]

        if standalone:
            lines += ["## スタンドアロン発言", ""]
            for msg in sorted(standalone, key=lambda x: x["datetime"]):
                t = msg["datetime"].strftime("%m/%d %H:%M")
                lines.append(f"- `{t}` {msg.get('text') or ''}")
            lines.append("")

        filename = self.slack_dir / f"{week_num}_{channel_name}.md"
        filename.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Slack KB保存: {filename.name}")

    # ------------------------------------------------------------------ #
    #  議事録ナレッジ
    # ------------------------------------------------------------------ #

    def _generate_meeting_knowledge(self, meeting_docs: list[dict]) -> None:
        self.meetings_dir.mkdir(parents=True, exist_ok=True)
        for doc in meeting_docs:
            try:
                self._save_meeting_file(doc)
            except Exception as e:
                logger.warning(f"議事録KB生成失敗 ({doc.get('title', '')}): {e}")

    def _save_meeting_file(self, doc: dict) -> None:
        date_str = doc["created_date"].strftime("%Y%m%d")
        title = doc["title"]
        # カレンダー添付の場合、ドキュメントタイトルが汎用的（"Gemini によるメモ" 等）な
        # ときはイベント名を使ってファイル名をユニークにする
        event_summary = doc.get("event_summary", "")
        if event_summary and ("Gemini によるメモ" in title or title.strip() == "メモ"):
            display_name = event_summary
        else:
            display_name = title
        safe_title = display_name[:40].replace("/", "_").replace("\\", "_").replace(":", "_")
        filename = self.meetings_dir / f"{date_str}_{safe_title}.md"

        # 同名ファイルが既に存在する場合はドキュメントIDのサフィックスを付与
        if filename.exists():
            doc_id_suffix = doc.get("id", "")[-6:]
            filename = self.meetings_dir / f"{date_str}_{safe_title}_{doc_id_suffix}.md"

        date_label = doc["created_date"].strftime("%Y-%m-%d")
        source_label = "Google Meet / Gemini 自動生成"
        if event_summary:
            source_label += f"（{event_summary}）"
        raw_text = doc.get("text", "")

        lines = [
            f"# {title}",
            "",
            f"**日時:** {date_label}",
            f"**ソース:** {source_label}",
            "",
            "---",
            "",
        ]

        # Gemini による要約セクション（生テキストの前に挿入）
        if self.gemini and self.gemini.enabled and raw_text:
            ai_summary = self.gemini.summarize_meeting(raw_text)
            if ai_summary:
                lines += [ai_summary, "", "---", "", "## 議事録原文", ""]

        lines.append(raw_text)

        filename.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"議事録KB保存: {filename.name}")

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
