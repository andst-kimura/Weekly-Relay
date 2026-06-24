"""
BacklogClient のリトライロジック単体テスト
requests をモック化してHTTP応答をシミュレート
"""
import pytest
from unittest.mock import MagicMock, patch, call
from requests.exceptions import HTTPError
from src.backlog_client import BacklogClient, _MAX_RETRIES


def make_client() -> BacklogClient:
    return BacklogClient(
        base_url="https://example.backlog.jp",
        api_key="test_key",
        my_user_id=123,
    )


def make_response(status_code: int, json_data=None, headers=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestRequestWithRetry:
    def test_success_on_first_attempt(self):
        client = make_client()
        ok_resp = make_response(200, {"id": 1})
        client.session.request = MagicMock(return_value=ok_resp)

        result = client._get("projects")
        assert result == {"id": 1}
        assert client.session.request.call_count == 1

    @patch("src.backlog_client.time.sleep")
    def test_retries_on_429(self, mock_sleep):
        client = make_client()
        rate_resp = make_response(429, headers={"Retry-After": "1"})
        ok_resp = make_response(200, [{"id": 1}])
        client.session.request = MagicMock(side_effect=[rate_resp, ok_resp])

        result = client._get("projects")
        assert result == [{"id": 1}]
        assert client.session.request.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("src.backlog_client.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        client = make_client()
        rate_resp = make_response(429, headers={"Retry-After": "1"})
        client.session.request = MagicMock(return_value=rate_resp)

        with pytest.raises(HTTPError):
            client._get("projects")

        # ループは _MAX_RETRIES 回実行され、最後に raise_for_status を呼ぶ
        assert client.session.request.call_count == _MAX_RETRIES

    @patch("src.backlog_client.time.sleep")
    def test_retries_on_503(self, mock_sleep):
        client = make_client()
        err_resp = make_response(503)
        ok_resp = make_response(200, {"ok": True})
        client.session.request = MagicMock(side_effect=[err_resp, ok_resp])

        result = client._get("projects")
        assert result == {"ok": True}

    def test_no_retry_on_404(self):
        client = make_client()
        not_found = make_response(404)
        client.session.request = MagicMock(return_value=not_found)

        with pytest.raises(HTTPError):
            client._get("issues/99999")

        assert client.session.request.call_count == 1
