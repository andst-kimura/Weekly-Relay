"""
DailySummary・build_daily_summary の単体テスト
"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock
from src.report_generator import ReportGenerator
from src.daily_summary import DailySummary


def make_generator() -> ReportGenerator:
    return ReportGenerator(claude_api_key="", claude_enabled=False)


def make_aggregated(has_backlog=True, has_slack=True) -> dict:
    gen = make_generator()
    backlog = []
    slack = []
    if has_backlog:
        backlog = [{
            "type": "assigned_issue", "project_name": "ストアアプリ",
            "project_key": "NEW_STORE_APP", "issue_id": 1,
            "issue_key": "NEW_STORE_APP-100", "summary": "本日のタスク",
            "status": "処理中", "parent_issue_id": None,
            "updated": "2026-06-22T09:00:00Z", "description": "",
        }]
    if has_slack:
        slack = [{
            "channel_id": "C001", "channel_name": "販売チーム_hblab",
            "text": "本日の進捗です。", "ts": 1750550400.0,
            "datetime": datetime(2026, 6, 22, 10, 0, 0),
            "thread_ts": None, "is_thread_reply": False, "reply_count": 0,
        }]
    today = datetime(2026, 6, 22, 0, 0, 0)
    now = datetime(2026, 6, 22, 17, 30, 0)
    return gen.aggregate(backlog, slack, [], today, now)


class TestBuildDailySummary:
    def test_contains_today_date(self):
        gen = make_generator()
        agg = make_aggregated()
        text = gen.build_daily_summary(agg)
        assert "2026/06/22" in text

    def test_contains_backlog_section(self):
        gen = make_generator()
        agg = make_aggregated(has_backlog=True, has_slack=False)
        text = gen.build_daily_summary(agg)
        assert "Backlog" in text
        assert "本日のタスク" in text

    def test_contains_slack_section(self):
        gen = make_generator()
        agg = make_aggregated(has_backlog=False, has_slack=True)
        text = gen.build_daily_summary(agg)
        assert "Slack" in text
        assert "販売チーム_hblab" in text

    def test_empty_shows_none_message(self):
        gen = make_generator()
        agg = make_aggregated(has_backlog=False, has_slack=False)
        text = gen.build_daily_summary(agg)
        assert "なし" in text

    def test_contains_auto_notice(self):
        gen = make_generator()
        agg = make_aggregated()
        text = gen.build_daily_summary(agg)
        assert "自動生成" in text


class TestDailySummaryRun:
    def test_run_calls_send_dm(self):
        backlog_client = MagicMock()
        backlog_client.get_my_projects.return_value = []
        backlog_client.get_all_my_activities.return_value = []

        slack_client = MagicMock()
        slack_client.get_my_channels.return_value = []
        slack_client.get_all_my_messages.return_value = []

        gen = make_generator()
        ds = DailySummary(backlog_client, slack_client, gen)
        ds.run()

        slack_client.send_dm.assert_called_once()

    def test_run_excludes_projects(self):
        backlog_client = MagicMock()
        backlog_client.get_my_projects.return_value = []
        backlog_client.get_all_my_activities.return_value = []

        slack_client = MagicMock()
        slack_client.get_my_channels.return_value = []
        slack_client.get_all_my_messages.return_value = []

        gen = make_generator()
        ds = DailySummary(backlog_client, slack_client, gen,
                          exclude_projects=["EXCLUDED"])
        ds.run()

        call_kwargs = backlog_client.get_all_my_activities.call_args[1]
        assert "EXCLUDED" in call_kwargs["exclude_projects"]
