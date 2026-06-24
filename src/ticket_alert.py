"""
未対応チケット警告モジュール
3営業日以上更新のない担当チケットを Slack DM で通知する
"""
from datetime import datetime, date, timedelta
import jpholiday
import logging

from src.backlog_client import BacklogClient
from src.slack_client import SlackClient
from src.backlog_poster import CLOSED_STATUSES

logger = logging.getLogger(__name__)


def count_business_days(start: date, end: date) -> int:
    """start の翌日から end までの営業日数（土日・祝日を除く）"""
    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5 and not jpholiday.is_holiday(current):
            days += 1
        current += timedelta(days=1)
    return days


class TicketAlert:
    def __init__(self, backlog_client: BacklogClient, slack_client: SlackClient,
                 exclude_projects: list[str] = None, stale_business_days: int = 3):
        self.backlog = backlog_client
        self.slack = slack_client
        self.exclude_projects = set(exclude_projects or [])
        self.stale_days = stale_business_days

    def get_stale_tickets(self) -> list[dict]:
        """3営業日以上更新のない担当チケットを取得"""
        projects = self.backlog.get_my_projects()
        if self.exclude_projects:
            projects = [p for p in projects if p["projectKey"] not in self.exclude_projects]

        today = datetime.now().date()
        stale: list[dict] = []

        for project in projects:
            try:
                tickets = self.backlog.get_all_assigned_issues(project["id"])
                for ticket in tickets:
                    status = ticket.get("status", {}).get("name", "")
                    if status in CLOSED_STATUSES:
                        continue

                    updated_str = ticket.get("updated", "")
                    if not updated_str:
                        continue
                    try:
                        updated_date = datetime.fromisoformat(
                            updated_str.replace("Z", "+00:00")
                        ).date()
                    except Exception:
                        continue

                    biz_days = count_business_days(updated_date, today)
                    if biz_days >= self.stale_days:
                        summary = ticket.get("summary", "")
                        stale.append({
                            "issue_key": ticket.get("issueKey", ""),
                            "summary": summary,
                            "status": status,
                            "updated_date": updated_date,
                            "business_days_stale": biz_days,
                            "project_name": project["name"],
                        })
            except Exception as e:
                logger.warning(f"プロジェクト '{project['name']}' の警告チェック失敗: {e}")

        stale.sort(key=lambda x: x["business_days_stale"], reverse=True)
        return stale

    def build_alert_message(self, stale_tickets: list[dict]) -> str:
        lines = [
            f"⚠️ *未対応チケット警告*（{len(stale_tickets)}件）",
            f"_{self.stale_days}営業日以上更新のない担当チケットです_",
            "",
        ]
        for t in stale_tickets:
            summary = t["summary"]
            if len(summary) > 40:
                summary = summary[:40] + "…"
            lines.append(
                f"• *{t['issue_key']}* `{t['business_days_stale']}営業日未更新`"
                f" | {t['status']} | {summary}"
            )
            lines.append(f"  最終更新: {t['updated_date']}")
        return "\n".join(lines)

    def run(self) -> None:
        logger.info("未対応チケット警告チェック開始")
        stale = self.get_stale_tickets()
        if not stale:
            logger.info("警告対象のチケットなし")
            return
        logger.info(f"警告対象: {len(stale)}件")
        msg = self.build_alert_message(stale)
        self.slack.send_dm(msg)
