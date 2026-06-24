"""
CleanupTool の単体テスト
"""
import pytest
from unittest.mock import MagicMock, patch
from src.cleanup import CleanupTool, _WR_SIGNATURE


def make_client(comments=None, parent_issues=None):
    client = MagicMock()
    client.get_parent_issues.return_value = parent_issues or [
        {"id": 100, "issueKey": "SALES_TEAM-27", "summary": "店舗ACE刷新"}
    ]
    client.get_all_comments.return_value = comments or []
    return client


class TestFindWeeklyRelayComments:
    def test_finds_wr_comments(self):
        comments = [
            {"id": 1, "content": f"進捗報告\n---\n*{_WR_SIGNATURE}*", "created": "2026-06-24T09:00:00Z"},
            {"id": 2, "content": "普通のコメント", "created": "2026-06-24T10:00:00Z"},
        ]
        client = make_client(comments=comments)
        tool = CleanupTool(client=client, report_project_key="SALES_TEAM")

        found = []
        parents = client.get_parent_issues("SALES_TEAM")
        for issue in parents:
            for c in client.get_all_comments(issue["id"]):
                if _WR_SIGNATURE in (c.get("content") or ""):
                    found.append(c)

        assert len(found) == 1
        assert found[0]["id"] == 1

    def test_no_wr_comments_returns_empty(self):
        comments = [
            {"id": 10, "content": "普通のコメント", "created": "2026-06-24T09:00:00Z"},
        ]
        client = make_client(comments=comments)

        found = [
            c for c in client.get_all_comments(100)
            if _WR_SIGNATURE in (c.get("content") or "")
        ]
        assert found == []


class TestDeleteComment:
    def test_delete_comment_calls_client(self):
        client = make_client()
        tool = CleanupTool(client=client, report_project_key="SALES_TEAM")
        client.delete_comment("SALES_TEAM-27", 1)
        client.delete_comment.assert_called_once_with("SALES_TEAM-27", 1)

    def test_delete_issue_calls_client(self):
        client = make_client()
        tool = CleanupTool(client=client, report_project_key="SALES_TEAM")
        client.delete_issue("SALES_TEAM-999")
        client.delete_issue.assert_called_once_with("SALES_TEAM-999")


class TestParseSelection:
    def _items(self):
        return [
            {"index": 1, "issue_key": "SALES_TEAM-27", "comment_id": 100},
            {"index": 2, "issue_key": "SALES_TEAM-254", "comment_id": 200},
            {"index": 3, "issue_key": "SALES_TEAM-37", "comment_id": 300},
        ]

    def test_all_returns_all_items(self):
        items = self._items()
        with patch("builtins.input", return_value="all"):
            result = CleanupTool._parse_selection(items)
        assert result == items

    def test_single_number(self):
        items = self._items()
        with patch("builtins.input", return_value="2"):
            result = CleanupTool._parse_selection(items)
        assert len(result) == 1
        assert result[0]["index"] == 2

    def test_comma_separated(self):
        items = self._items()
        with patch("builtins.input", return_value="1,3"):
            result = CleanupTool._parse_selection(items)
        assert len(result) == 2
        assert {r["index"] for r in result} == {1, 3}

    def test_q_returns_empty(self):
        items = self._items()
        with patch("builtins.input", return_value="q"):
            result = CleanupTool._parse_selection(items)
        assert result == []

    def test_invalid_number_skipped(self):
        items = self._items()
        with patch("builtins.input", return_value="99"):
            result = CleanupTool._parse_selection(items)
        assert result == []
