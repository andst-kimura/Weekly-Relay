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
_TTL_DAYS_CONTEXT = 30

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
            now = datetime.now(timezone.utc)
            payload = {
                **data,
                "saved_at": now,
                "expire_at": now + timedelta(days=_TTL_DAYS_CONTEXT),
            }
            _set_doc("context_snapshots", doc_id, payload)
        except Exception as e:
            logger.warning(f"Firestore save 失敗 ({doc_id}): {e}")

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
