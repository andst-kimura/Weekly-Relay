"""
日次夕方サマリーモジュール
当日の Backlog 活動と Slack 発言をまとめて自分宛 Slack DM に送信する
"""
from datetime import datetime
import logging

from src.backlog_client import BacklogClient
from src.slack_client import SlackClient
from src.report_generator import ReportGenerator

logger = logging.getLogger(__name__)


class DailySummary:
    def __init__(self, backlog_client: BacklogClient, slack_client: SlackClient,
                 generator: ReportGenerator, exclude_projects: list[str] = None,
                 gemini_client=None):
        self.backlog = backlog_client
        self.slack = slack_client
        self.generator = generator
        self.gemini = gemini_client
        self.exclude_projects = exclude_projects or []

    def run(self) -> None:
        logger.info("日次夕方サマリー開始")

        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        now = datetime.now()

        logger.info(f"対象期間: {today_start} 〜 {now}")

        activities = self.backlog.get_all_my_activities(
            today_start, now, exclude_projects=self.exclude_projects
        )
        logger.info(f"Backlog: {len(activities)}件の活動を取得")

        messages = self.slack.get_all_my_messages(today_start, now)
        logger.info(f"Slack: {len(messages)}件のメッセージを取得")

        aggregated = self.generator.aggregate(activities, messages, [], today_start, now)

        # Gemini が有効な場合は自然文サマリー、無効ならルールベース
        if self.gemini and self.gemini.enabled and (activities or messages):
            text = self._build_gemini_summary(aggregated, activities, messages, now)
        else:
            text = self.generator.build_daily_summary(aggregated)

        self.slack.send_dm(text)
        logger.info("日次夕方サマリー完了")

    def _build_gemini_summary(self, aggregated: dict, activities: list[dict],
                               messages: list[dict], now: datetime) -> str:
        """Gemini で当日のサマリーを自然文生成する"""
        date_str = now.strftime("%Y年%m月%d日（%a）")
        lines = []

        if activities:
            lines.append("【Backlog 活動】")
            for act in activities[:20]:
                status = act.get("status", "")
                lines.append(f"- [{act['project_key']}] {act['summary']}（{status}）")

        if messages:
            lines.append("\n【Slack 発言】")
            for msg in messages[:20]:
                ch = msg.get("channel_name", "")
                text = (msg.get("text") or "")[:100].replace("\n", " ")
                lines.append(f"- #{ch}: {text}")

        activities_text = "\n".join(lines)
        result = self.gemini.build_daily_summary(date_str, activities_text)

        # Gemini 失敗時はルールベースにフォールバック
        return result or self.generator.build_daily_summary(aggregated)
