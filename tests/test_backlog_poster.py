"""
BacklogPoster の単体テスト
BacklogClient をモック化して転記ロジックを検証
"""
import pytest
from unittest.mock import MagicMock, patch
from src.backlog_poster import BacklogPoster, CLOSED_STATUSES


def make_mock_client(issue_status: str = "処理中") -> MagicMock:
    client = MagicMock()
    client.get_issue.return_value = {
        "id": 100,
        "issueKey": "SALES_TEAM-27",
        "status": {"name": issue_status},
    }
    client.get_project.return_value = {"id": 1, "projectKey": "SALES_TEAM"}
    client.get_issue_types.return_value = [{"id": 10, "name": "タスク"}]
    return client


def make_poster(client=None, dry_run=False) -> BacklogPoster:
    return BacklogPoster(
        client=client or make_mock_client(),
        report_project_key="SALES_TEAM",
        channel_mapping={},
        dry_run=dry_run,
    )


class TestClosedStatuses:
    def test_constant_contains_japanese_statuses(self):
        assert "完了" in CLOSED_STATUSES
        assert "クローズ" in CLOSED_STATUSES

    def test_constant_contains_english_statuses(self):
        assert "Done" in CLOSED_STATUSES
        assert "Closed" in CLOSED_STATUSES


class TestPostComment:
    def test_dry_run_skips_actual_post(self):
        client = make_mock_client()
        poster = make_poster(client=client, dry_run=True)
        result = poster._post_comment("SALES_TEAM-27", "テストコメント")
        assert result["action"] == "comment_skipped_dry_run"
        client.add_comment_to_issue.assert_not_called()

    def test_actual_post_calls_client(self):
        client = make_mock_client()
        poster = make_poster(client=client, dry_run=False)
        poster._post_comment("SALES_TEAM-27", "テストコメント")
        client.add_comment_to_issue.assert_called_once_with(100, "テストコメント")

    def test_returns_error_on_exception(self):
        client = make_mock_client()
        client.get_issue.side_effect = Exception("API Error")
        poster = make_poster(client=client)
        result = poster._post_comment("SALES_TEAM-27", "テスト")
        assert result["action"] == "error"


class TestPostWeeklyReport:
    def _base_args(self):
        from datetime import datetime
        return dict(
            comment_text="テストレポート",
            backlog_activities=[],
            slack_messages=[],
            week_start=datetime(2026, 6, 15),
            week_end=datetime(2026, 6, 19, 18),
        )

    def test_skips_closed_issue(self):
        client = make_mock_client(issue_status="完了")
        poster = BacklogPoster(
            client=client,
            report_project_key="SALES_TEAM",
            channel_mapping={"販売チーム_hblab": {"parent_issue_key": "SALES_TEAM-27", "label": "test", "project_key": ""}},
            dry_run=True,
        )
        from datetime import datetime
        slack_messages = [{
            "channel_id": "C001", "channel_name": "販売チーム_hblab",
            "text": "test", "ts": 0.0,
            "datetime": datetime(2026, 6, 17), "thread_ts": None,
            "is_thread_reply": False, "reply_count": 0,
        }]
        results = poster.post_weekly_report(
            "テスト", [], slack_messages,
            datetime(2026, 6, 15), datetime(2026, 6, 19, 18),
        )
        assert all(r.get("action") != "commented" for r in results)

    def test_project_key_filter_uses_config(self):
        """SALES_TEAM に属さない課題は転記されないこと"""
        client = MagicMock()
        client.get_issue.return_value = {
            "id": 200,
            "issueKey": "OTHER_PROJECT-1",
            "status": {"name": "処理中"},
        }
        # OTHER_PROJECT の課題は SALES_TEAM 親課題に解決できない
        client.resolve_sales_team_parent.return_value = None
        poster = BacklogPoster(
            client=client,
            report_project_key="SALES_TEAM",
            channel_mapping={},
            dry_run=True,
        )
        from datetime import datetime
        activities = [{
            "type": "assigned_issue", "project_name": "Other", "project_key": "OTHER_PROJECT",
            "issue_id": 99, "issue_key": "OTHER_PROJECT-5", "summary": "test",
            "status": "処理中", "parent_issue_id": 200,
            "updated": "", "description": "",
        }]
        results = poster.post_weekly_report(
            "テスト", activities, [],
            datetime(2026, 6, 15), datetime(2026, 6, 19, 18),
        )
        assert results == []
