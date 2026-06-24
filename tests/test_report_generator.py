"""
ReportGenerator の単体テスト
外部API呼び出しなし、データ変換・テキスト生成ロジックを検証
"""
import pytest
from datetime import datetime
from src.report_generator import ReportGenerator


WEEK_START = datetime(2026, 6, 15, 0, 0, 0)
WEEK_END = datetime(2026, 6, 19, 18, 0, 0)


def make_generator() -> ReportGenerator:
    return ReportGenerator(claude_api_key="", claude_enabled=False)


def make_backlog_activities() -> list[dict]:
    return [
        {
            "type": "assigned_issue",
            "project_name": "ストアアプリ",
            "project_key": "NEW_STORE_APP",
            "issue_id": 1,
            "issue_key": "NEW_STORE_APP-100",
            "summary": "テスト課題",
            "status": "処理中",
            "parent_issue_id": None,
            "updated": "2026-06-17T09:00:00Z",
            "description": "テスト用の説明文です。",
        },
        {
            "type": "comment",
            "project_name": "ストアアプリ",
            "project_key": "NEW_STORE_APP",
            "issue_id": 1,
            "issue_key": "NEW_STORE_APP-100",
            "summary": "テスト課題",
            "status": "処理中",
            "parent_issue_id": None,
            "comment_id": 999,
            "comment_content": "対応完了しました。",
            "updated": "2026-06-17T10:30:00Z",
        },
    ]


def make_slack_messages() -> list[dict]:
    return [
        {
            "channel_id": "C001",
            "channel_name": "販売チーム_hblab",
            "text": "本日の進捗を共有します。",
            "ts": 1750150200.0,
            "datetime": datetime(2026, 6, 17, 10, 30, 0),
            "thread_ts": None,
            "is_thread_reply": False,
            "reply_count": 0,
        },
    ]


def make_calendar_events() -> list[dict]:
    return [
        {
            "calendar_id": "primary",
            "event_id": "evt1",
            "summary": "定例MTG",
            "description": "",
            "start_dt": datetime(2026, 6, 17, 10, 0, 0),
            "end_dt": datetime(2026, 6, 17, 11, 0, 0),
            "duration_minutes": 60,
            "duration_hours": 1.0,
            "is_all_day": False,
            "location": "",
            "attendees_count": 3,
            "my_status": "accepted",
        },
    ]


class TestAggregate:
    def test_backlog_grouped_by_project(self):
        gen = make_generator()
        result = gen.aggregate(make_backlog_activities(), [], [], WEEK_START, WEEK_END)
        assert "ストアアプリ" in result["backlog_by_project"]

    def test_slack_grouped_by_channel(self):
        gen = make_generator()
        result = gen.aggregate([], make_slack_messages(), [], WEEK_START, WEEK_END)
        assert "販売チーム_hblab" in result["slack_by_channel"]

    def test_calendar_hours_summed(self):
        gen = make_generator()
        result = gen.aggregate([], [], make_calendar_events(), WEEK_START, WEEK_END)
        assert result["total_calendar_hours"] == 1.0

    def test_empty_inputs(self):
        gen = make_generator()
        result = gen.aggregate([], [], [], WEEK_START, WEEK_END)
        assert result["backlog_by_project"] == {}
        assert result["slack_by_channel"] == {}
        assert result["total_calendar_hours"] == 0


class TestBuildBacklogComment:
    def _aggregate(self, backlog=None, slack=None, cal=None):
        gen = make_generator()
        return gen, gen.aggregate(
            backlog or [], slack or [], cal or [], WEEK_START, WEEK_END
        )

    def test_contains_week_header(self):
        gen, agg = self._aggregate()
        text = gen.build_backlog_comment(agg)
        assert "2026/06/15" in text
        assert "2026/06/19" in text

    def test_contains_backlog_section(self):
        gen, agg = self._aggregate(backlog=make_backlog_activities())
        text = gen.build_backlog_comment(agg)
        assert "Backlog" in text
        assert "テスト課題" in text

    def test_contains_slack_section(self):
        gen, agg = self._aggregate(slack=make_slack_messages())
        text = gen.build_backlog_comment(agg)
        assert "Slack" in text
        assert "本日の進捗" in text

    def test_contains_calendar_section(self):
        gen, agg = self._aggregate(cal=make_calendar_events())
        text = gen.build_backlog_comment(agg)
        assert "工数" in text
        assert "1.0" in text

    def test_contains_auto_post_notice(self):
        gen, agg = self._aggregate()
        text = gen.build_backlog_comment(agg)
        assert "自動転記" in text

    def test_comment_truncated_at_150_chars(self):
        long_comment = "あ" * 200
        activities = make_backlog_activities()
        activities[1]["comment_content"] = long_comment
        gen, agg = self._aggregate(backlog=activities)
        text = gen.build_backlog_comment(agg)
        assert "…" in text


class TestBuildCommentForIssue:
    def test_filters_to_specified_project(self):
        gen = make_generator()
        agg = gen.aggregate(make_backlog_activities(), make_slack_messages(), [], WEEK_START, WEEK_END)
        text = gen.build_comment_for_issue("SALES_TEAM-27", ["NEW_STORE_APP"], ["販売チーム_hblab"], agg)
        assert "テスト課題" in text
        assert "本日の進捗" in text
