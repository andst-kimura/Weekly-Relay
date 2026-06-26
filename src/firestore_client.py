"""
Firestore クライアント（REST 実装）
google-cloud-firestore SDK の gRPC を使わず、Firestore REST API を直接呼ぶ。
社内プロキシ環境では gRPC の boringSSL が証明書検証に失敗するため、
Python 標準の requests（urllib3 + certifi）を使用して SSL 問題を回避する。
"""
import os
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import google.auth
import google.auth.transport.requests

logger = logging.getLogger(__name__)

_TTL_DAYS_SYNC_LOG = 90

_FIRESTORE_BASE = "https://firestore.googleapis.com/v1"


# --------------------------------------------------------------------------- #
#  Firestore REST 値変換
# --------------------------------------------------------------------------- #

def _to_fs(val) -> dict:
    """Python 値 → Firestore REST フィールド値"""
    if val is None:
        return {"nullValue": None}
    if isinstance(val, bool):
        return {"booleanValue": val}
    if isinstance(val, int):
        return {"integerValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if isinstance(val, str):
        return {"stringValue": val}
    if isinstance(val, datetime):
        ts = val
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return {"timestampValue": ts.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")}
    if isinstance(val, dict):
        return {"mapValue": {"fields": {k: _to_fs(v) for k, v in val.items()}}}
    if isinstance(val, list):
        return {"arrayValue": {"values": [_to_fs(v) for v in val]}}
    return {"stringValue": str(val)}


def _from_fs(val: dict):
    """Firestore REST フィールド値 → Python 値"""
    if "nullValue" in val:
        return None
    if "booleanValue" in val:
        return val["booleanValue"]
    if "integerValue" in val:
        return int(val["integerValue"])
    if "doubleValue" in val:
        return float(val["doubleValue"])
    if "stringValue" in val:
        return val["stringValue"]
    if "timestampValue" in val:
        ts = val["timestampValue"]
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return ts
        return ts
    if "mapValue" in val:
        return {k: _from_fs(v) for k, v in val["mapValue"].get("fields", {}).items()}
    if "arrayValue" in val:
        return [_from_fs(v) for v in val["arrayValue"].get("values", [])]
    return None


def _doc_to_dict(doc: dict) -> dict | None:
    """Firestore REST ドキュメント → Python dict"""
    fields = doc.get("fields")
    if not fields:
        return None
    return {k: _from_fs(v) for k, v in fields.items()}


# --------------------------------------------------------------------------- #
#  認証済み HTTP セッション（シングルトン）
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _get_session() -> google.auth.transport.requests.AuthorizedSession:
    """google.auth の AuthorizedSession を返す（requests ベース・Python SSL 使用）"""
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/datastore"]
    )
    return google.auth.transport.requests.AuthorizedSession(creds)


def _doc_path(project: str, database: str, collection: str, doc_id: str) -> str:
    db = database if database.startswith("(") or "/" in database else database
    return (
        f"{_FIRESTORE_BASE}/projects/{project}/databases/{db}"
        f"/documents/{collection}/{doc_id}"
    )


def _col_path(project: str, database: str, collection: str) -> str:
    db = database if database.startswith("(") or "/" in database else database
    return (
        f"{_FIRESTORE_BASE}/projects/{project}/databases/{db}"
        f"/documents/{collection}"
    )


# --------------------------------------------------------------------------- #
#  低レベル操作
# --------------------------------------------------------------------------- #

def _get_doc(collection: str, doc_id: str) -> dict | None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
    database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
    url = _doc_path(project, database, collection, doc_id)
    resp = _get_session().get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return _doc_to_dict(resp.json())


def _set_doc(collection: str, doc_id: str, data: dict) -> None:
    """PATCH で upsert（全フィールド上書き）"""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
    database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
    url = _doc_path(project, database, collection, doc_id)
    body = {"fields": {k: _to_fs(v) for k, v in data.items()}}
    resp = _get_session().patch(url, json=body, timeout=30)
    resp.raise_for_status()


def _add_doc(collection: str, data: dict) -> None:
    """POST でオート ID ドキュメント追加"""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
    database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
    url = _col_path(project, database, collection)
    body = {"fields": {k: _to_fs(v) for k, v in data.items()}}
    resp = _get_session().post(url, json=body, timeout=30)
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
#  公開クライアントクラス
# --------------------------------------------------------------------------- #

class FirestoreClient:
    """Weekly Relay 用 Firestore CRUD ラッパー（REST 実装）"""

    # ------------------------------------------------------------------ #
    #  context_snapshots（チケット / Slack / 議事録 KB）
    # ------------------------------------------------------------------ #

    def get_context_snapshot(self, doc_id: str) -> dict | None:
        try:
            return _get_doc("context_snapshots", doc_id)
        except Exception as e:
            logger.warning(f"Firestore get 失敗 ({doc_id}): {e}")
            return None

    def save_context_snapshot(self, doc_id: str, data: dict) -> None:
        try:
            payload = {**data, "saved_at": datetime.now(timezone.utc)}
            _set_doc("context_snapshots", doc_id, payload)
        except Exception as e:
            logger.warning(f"Firestore save 失敗 ({doc_id}): {e}")

    def list_context_snapshots(self, page_size: int = 100) -> list[tuple[str, dict]]:
        """context_snapshots の全ドキュメントを返す（--sync-vectors 用）"""
        try:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
            database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
            url = _col_path(project, database, "context_snapshots")
            params: dict = {"pageSize": page_size}
            results: list[tuple[str, dict]] = []
            while True:
                resp = _get_session().get(url, params=params, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                for doc in body.get("documents", []):
                    doc_id = doc["name"].split("/")[-1]
                    doc_data = _doc_to_dict(doc)
                    if doc_data:
                        results.append((doc_id, doc_data))
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
                params["pageToken"] = page_token
            return results
        except Exception as e:
            logger.warning(f"Firestore list 失敗: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  weekly_reports
    # ------------------------------------------------------------------ #

    def save_weekly_report(self, week_start: datetime, week_end: datetime,
                            content: str) -> None:
        try:
            doc_id = week_start.strftime("%Y%m%d")
            _set_doc("weekly_reports", doc_id, {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "content": content,
                "saved_at": datetime.now(timezone.utc),
            })
            logger.info(f"Firestore 週次レポート保存: weekly_reports/{doc_id}")
        except Exception as e:
            logger.warning(f"Firestore 週次レポート保存失敗: {e}")


    # ------------------------------------------------------------------ #
    #  calendar_reports
    # ------------------------------------------------------------------ #

    def save_calendar_report(self, first_event_date: datetime, content: str) -> None:
        try:
            doc_id = first_event_date.strftime("%Y%m%d")
            _set_doc("calendar_reports", doc_id, {
                "content": content,
                "saved_at": datetime.now(timezone.utc),
            })
            logger.info(f"Firestore カレンダーレポート保存: calendar_reports/{doc_id}")
        except Exception as e:
            logger.warning(f"Firestore カレンダーレポート保存失敗: {e}")


    # ------------------------------------------------------------------ #
    #  manual_memos（Slack Bot 手動メモ）
    # ------------------------------------------------------------------ #

    def save_manual_memo(self, text: str, parent_issue_key: str = "",
                          created_by: str = "", channel: str = "") -> str:
        """手動メモを manual_memos コレクションに保存してドキュメントIDを返す"""
        try:
            now = datetime.now(timezone.utc)
            ts_ms = int(now.timestamp() * 1000)
            doc_id = f"memo_{now.strftime('%Y%m%d')}_{ts_ms}"
            _set_doc("manual_memos", doc_id, {
                "text": text,
                "parent_issue_key": parent_issue_key,
                "created_by": created_by,
                "channel": channel,
                "created_at": now,
                "week_key": now.strftime("%Y%W"),
            })
            logger.info(f"手動メモ保存: {doc_id} parent={parent_issue_key or '未指定'}")
            return doc_id
        except Exception as e:
            logger.warning(f"手動メモ保存失敗: {e}")
            return ""

    def get_manual_memos(self, since: datetime, until: datetime) -> list[dict]:
        """指定期間に作成された手動メモを返す"""
        try:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
            database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
            url = _col_path(project, database, "manual_memos")
            results = []
            params: dict = {"pageSize": 200}
            while True:
                resp = _get_session().get(url, params=params, timeout=30)
                resp.raise_for_status()
                body = resp.json()
                for doc in body.get("documents", []):
                    data = _doc_to_dict(doc)
                    if not data:
                        continue
                    created_at = data.get("created_at")
                    if isinstance(created_at, datetime):
                        if since.tzinfo and created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        elif not since.tzinfo and created_at.tzinfo:
                            created_at = created_at.replace(tzinfo=None)
                        if since <= created_at <= until:
                            results.append(data)
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
                params["pageToken"] = page_token
            logger.info(f"手動メモ取得: {len(results)} 件（{since.date()}〜{until.date()}）")
            return results
        except Exception as e:
            logger.warning(f"手動メモ取得失敗: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  shared_infos
    # ------------------------------------------------------------------ #

    def save_shared_info(self, issue_key: str, summary: str, body: str,
                          slack_user_id: str = "", channel: str = "",
                          due_date: str = "") -> str:
        """共有事項（Backlog 起票済み）を shared_infos コレクションに保存する"""
        try:
            now = datetime.now(timezone.utc)
            ts_ms = int(now.timestamp() * 1000)
            doc_id = f"shared_{now.strftime('%Y%m%d')}_{ts_ms}"
            _set_doc("shared_infos", doc_id, {
                "issue_key": issue_key,
                "summary": summary,
                "body": body,
                "slack_user_id": slack_user_id,
                "channel": channel,
                "due_date": due_date,
                "created_at": now,
                "week_key": now.strftime("%Y%W"),
            })
            logger.info(f"共有事項保存: {doc_id} issue={issue_key}")
            return doc_id
        except Exception as e:
            logger.warning(f"共有事項保存失敗: {e}")
            return ""

    # ------------------------------------------------------------------ #
    #  sync_logs
    # ------------------------------------------------------------------ #

    def write_sync_log(self, status: str, job: str, detail: str = "",
                        duration_sec: float = 0.0) -> None:
        try:
            now = datetime.now(timezone.utc)
            _add_doc("sync_logs", {
                "job": job,
                "status": status,
                "detail": detail,
                "duration_sec": duration_sec,
                "created_at": now,
                "expire_at": now + timedelta(days=_TTL_DAYS_SYNC_LOG),
            })
        except Exception as e:
            logger.warning(f"Firestore sync_log 書き込み失敗: {e}")
