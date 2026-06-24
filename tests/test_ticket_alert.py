"""
TicketAlert の単体テスト
"""
import pytest
from datetime import date
from unittest.mock import MagicMock
from src.ticket_alert import TicketAlert, count_business_days
from src.backlog_poster import CLOSED_STATUSES


class TestCountBusinessDays:
    def test_same_day_is_zero(self):
        d = date(2026, 6, 15)  # 月曜
        assert count_business_days(d, d) == 0

    def test_monday_to_thursday(self):
        # 月→火→水→木 = 3営業日
        assert count_business_days(date(2026, 6, 15), date(2026, 6, 18)) == 3

    def test_skips_weekend(self):
        # 金曜 → 月曜 = 1営業日（土日スキップ）
        assert count_business_days(date(2026, 6, 19), date(2026, 6, 22)) == 1

    def test_full_week(self):
        # 月曜 → 翌月曜 = 5営業日
        assert count_business_days(date(2026, 6, 15), date(2026, 6, 22)) == 5

    def test_skips_holiday(self):
        # 2026-01-01（元日）を含む場合、その日はカウントされない
        # 2025-12-31（水）→ 2026-01-02（金）= 元日スキップで1営業日
        assert count_business_days(date(2025, 12, 31), date(2026, 1, 2)) == 1


class TestBuildAlertMessage:
    def _make_alert(self) -> TicketAlert:
        return TicketAlert(
            backlog_client=MagicMock(),
            slack_client=MagicMock(),
            stale_business_days=3,
        )

    def test_contains_ticket_key(self):
        alert = self._make_alert()
        stale = [{
            "issue_key": "SALES_TEAM-27",
            "summary": "テストチケット",
            "status": "処理中",
            "updated_date": date(2026, 6, 15),
            "business_days_stale": 5,
            "project_name": "販売チーム！",
        }]
        msg = alert.build_alert_message(stale)
        assert "SALES_TEAM-27" in msg
        assert "5営業日未更新" in msg

    def test_contains_count(self):
        alert = self._make_alert()
        stale = [
            {"issue_key": f"T-{i}", "summary": "x", "status": "処理中",
             "updated_date": date(2026, 6, 10), "business_days_stale": i + 3,
             "project_name": "PJ"}
            for i in range(3)
        ]
        msg = alert.build_alert_message(stale)
        assert "3件" in msg

    def test_long_summary_truncated(self):
        alert = self._make_alert()
        stale = [{
            "issue_key": "T-1",
            "summary": "あ" * 60,
            "status": "処理中",
            "updated_date": date(2026, 6, 1),
            "business_days_stale": 10,
            "project_name": "PJ",
        }]
        msg = alert.build_alert_message(stale)
        assert "…" in msg


class TestGetStaleTickets:
    def test_excludes_closed_statuses(self):
        client = MagicMock()
        client.get_my_projects.return_value = [
            {"id": 1, "name": "PJ", "projectKey": "PJ"}
        ]
        # 完了ステータスのチケットは除外される
        client.get_all_assigned_issues.return_value = [
            {"issueKey": "PJ-1", "summary": "完了済み",
             "status": {"name": "完了"}, "updated": "2020-01-01T00:00:00Z"},
        ]
        alert = TicketAlert(client, MagicMock(), stale_business_days=3)
        stale = alert.get_stale_tickets()
        assert stale == []

    def test_includes_stale_ticket(self):
        client = MagicMock()
        client.get_my_projects.return_value = [
            {"id": 1, "name": "PJ", "projectKey": "PJ"}
        ]
        client.get_all_assigned_issues.return_value = [
            {"issueKey": "PJ-1", "summary": "古いチケット",
             "status": {"name": "処理中"}, "updated": "2020-01-01T00:00:00Z"},
        ]
        alert = TicketAlert(client, MagicMock(), stale_business_days=3)
        stale = alert.get_stale_tickets()
        assert len(stale) == 1
        assert stale[0]["issue_key"] == "PJ-1"

    def test_exclude_projects_respected(self):
        client = MagicMock()
        client.get_my_projects.return_value = [
            {"id": 1, "name": "PJ1", "projectKey": "EXCLUDED"},
            {"id": 2, "name": "PJ2", "projectKey": "INCLUDED"},
        ]
        client.get_all_assigned_issues.return_value = []
        alert = TicketAlert(client, MagicMock(), exclude_projects=["EXCLUDED"])
        alert.get_stale_tickets()
        # get_all_assigned_issues は INCLUDED のプロジェクト（id=2）だけ呼ばれる
        client.get_all_assigned_issues.assert_called_once_with(2)
