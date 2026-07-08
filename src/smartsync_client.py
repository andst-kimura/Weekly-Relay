"""
SmartSync Firestore クライアント

SmartSync の context_snapshots コレクションを読み取り、
embedding フィールドを書き込む（Firestore Vector Search 用）。
社内プロキシ対応のため gRPC ではなく REST API を使用。
"""
import os
import logging
from functools import lru_cache
from typing import Iterator

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import google.auth
import google.auth.transport.requests
import google.oauth2.service_account

logger = logging.getLogger(__name__)

_FIRESTORE_BASE = "https://firestore.googleapis.com/v1"
_PROJECT   = "andst-hd-ax"


def _database() -> str:
    return os.environ.get("SMARTSYNC_FIRESTORE_DATABASE", "smart-sync-stg")


@lru_cache(maxsize=1)
def _get_session() -> google.auth.transport.requests.AuthorizedSession:
    scopes = ["https://www.googleapis.com/auth/datastore"]
    sa_key = os.environ.get(
        "SMARTSYNC_GOOGLE_APPLICATION_CREDENTIALS",
        r"config/andst-hd-ax-276f47e40899.json",
    )
    # 相対パスはリポジトリルート基準で解決（webapp 等 cwd が異なる場合に対応）
    if not os.path.isabs(sa_key) and not os.path.exists(sa_key):
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(_root, sa_key)
        if os.path.exists(candidate):
            sa_key = candidate
    if os.path.exists(sa_key):
        # ローカル PC: サービスアカウントキー（社内プロキシ対応で verify=False）
        creds = google.oauth2.service_account.Credentials.from_service_account_file(
            sa_key, scopes=scopes)
        session = google.auth.transport.requests.AuthorizedSession(creds)
        session.verify = False
        return session
    # Cloud Run 等: ADC（Application Default Credentials）
    creds, _ = google.auth.default(scopes=scopes)
    return google.auth.transport.requests.AuthorizedSession(creds)


def _col_url(collection: str) -> str:
    db = _database()
    return f"{_FIRESTORE_BASE}/projects/{_PROJECT}/databases/{db}/documents/{collection}"


def _doc_url(collection: str, doc_id: str) -> str:
    return f"{_col_url(collection)}/{doc_id}"


# --------------------------------------------------------------------------- #
#  読み取り
# --------------------------------------------------------------------------- #

def list_context_snapshots(page_size: int = 300) -> list[tuple[str, dict]]:
    """context_snapshots の全ドキュメントを返す。embedding 済みのものも含む。"""
    url = _col_url("context_snapshots")
    params: dict = {"pageSize": page_size}
    results: list[tuple[str, dict]] = []
    session = _get_session()
    while True:
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        for doc in body.get("documents", []):
            doc_id = doc["name"].split("/")[-1]
            data   = _parse_doc(doc)
            if data:
                results.append((doc_id, data))
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return results


def list_context_snapshots_without_embedding(page_size: int = 300) -> list[tuple[str, dict]]:
    """embedding フィールドがまだ存在しないドキュメントのみ返す（差分更新用）"""
    all_docs = list_context_snapshots(page_size)
    return [(doc_id, data) for doc_id, data in all_docs if "embedding" not in data]


def get_context_snapshot(doc_id: str) -> dict | None:
    """context_snapshots から 1 件取得する（存在しなければ None）"""
    url = _doc_url("context_snapshots", doc_id)
    resp = _get_session().get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return _parse_doc(resp.json())


# --------------------------------------------------------------------------- #
#  書き込み（ドキュメント全体 / embedding フィールド）
# --------------------------------------------------------------------------- #

def _to_fs(val) -> dict:
    """Python 値 → Firestore REST フィールド値"""
    from datetime import datetime, timezone
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
        ts = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return {"timestampValue": ts.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")}
    if isinstance(val, dict):
        return {"mapValue": {"fields": {k: _to_fs(v) for k, v in val.items()}}}
    if isinstance(val, list):
        return {"arrayValue": {"values": [_to_fs(v) for v in val]}}
    return {"stringValue": str(val)}


def save_doc(collection: str, doc_id: str, data: dict) -> None:
    """任意コレクションにドキュメントを upsert する。

    PATCH + updateMask で data のフィールドのみ更新するため、
    既存の他フィールド（embedding 等）を消さずに更新できる。
    """
    url = _doc_url(collection, doc_id)
    fields = {k: _to_fs(v) for k, v in data.items() if k != "doc_id"}
    fields["doc_id"] = _to_fs(doc_id)
    params = [("updateMask.fieldPaths", k) for k in fields.keys()]
    body = {"fields": fields}
    resp = _get_session().patch(url, json=body, params=params, timeout=30)
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"ドキュメント書き込み失敗 ({collection}/{doc_id}): {resp.status_code} {resp.text[:300]}"
        )


def add_doc(collection: str, data: dict) -> None:
    """任意コレクションに自動 ID でドキュメントを追加する。"""
    url = _col_url(collection)
    body = {"fields": {k: _to_fs(v) for k, v in data.items()}}
    resp = _get_session().post(url, json=body, timeout=30)
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"ドキュメント追加失敗 ({collection}): {resp.status_code} {resp.text[:300]}"
        )


def list_docs(collection: str, page_size: int = 200) -> list[tuple[str, dict]]:
    """任意コレクションの全ドキュメントを (doc_id, data) で返す。"""
    url = _col_url(collection)
    params: dict = {"pageSize": page_size}
    results: list[tuple[str, dict]] = []
    session = _get_session()
    while True:
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        for doc in body.get("documents", []):
            doc_id = doc["name"].split("/")[-1]
            data = _parse_doc(doc)
            if data:
                results.append((doc_id, data))
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return results


def save_context_snapshot(doc_id: str, data: dict) -> None:
    """context_snapshots にドキュメントを upsert する（embedding は保持される）。"""
    save_doc("context_snapshots", doc_id, data)


def _vector_value(embedding: list[float]) -> dict:
    """Firestore Vector 型の REST 表現を返す。
    arrayValue ではなく mapValue(__type__=__vector__) が必要。
    """
    return {
        "mapValue": {
            "fields": {
                "__type__": {"stringValue": "__vector__"},
                "value": {
                    "arrayValue": {
                        "values": [{"doubleValue": v} for v in embedding]
                    }
                }
            }
        }
    }


def write_embedding(doc_id: str, embedding: list[float]) -> None:
    """既存ドキュメントに embedding フィールドを PATCH で追記する。"""
    url = _doc_url("context_snapshots", doc_id)
    # updateMask で embedding フィールドのみ更新（他フィールドを破壊しない）
    params = {"updateMask.fieldPaths": "embedding"}
    body = {
        "fields": {
            "embedding": _vector_value(embedding)
        }
    }
    resp = _get_session().patch(url, json=body, params=params, timeout=30)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"embedding 書き込み失敗 ({doc_id}): {resp.status_code} {resp.text[:200]}")


# --------------------------------------------------------------------------- #
#  Vector Search
# --------------------------------------------------------------------------- #

def vector_search(embedding: list[float], n_results: int = 5) -> list[dict]:
    """Firestore Vector Search（findNearest）で類似ドキュメントを返す。"""
    db = _database()
    url = (
        f"{_FIRESTORE_BASE}/projects/{_PROJECT}/databases/{db}"
        f"/documents:runQuery"
    )
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "context_snapshots"}],
            "findNearest": {
                "vectorField": {"fieldPath": "embedding"},
                "queryVector": _vector_value(embedding),
                "distanceMeasure": "COSINE",
                "limit": n_results,
            },
        }
    }
    resp = _get_session().post(url, json=body, timeout=30)
    resp.raise_for_status()
    results = []
    for item in resp.json():
        doc = item.get("document")
        if not doc:
            continue
        doc_id = doc["name"].split("/")[-1]
        data   = _parse_doc(doc)
        # Firestore COSINE は類似度（0〜1）を直接返す（距離ではない）
        distance = item.get("distance", 0.0)
        score = round(float(distance), 4)
        results.append({"doc_id": doc_id, "score": score, "data": data})
    return results


def count_with_embedding() -> int:
    """embedding フィールドを持つドキュメント数を返す。"""
    all_docs = list_context_snapshots()
    return sum(1 for _, data in all_docs if "embedding" in data)


# --------------------------------------------------------------------------- #
#  内部ユーティリティ
# --------------------------------------------------------------------------- #

def _parse_doc(doc: dict) -> dict | None:
    """Firestore REST ドキュメント → Python dict"""
    fields = doc.get("fields")
    if not fields:
        return None
    return {k: _from_fs(v) for k, v in fields.items()}


def _from_fs(val: dict):
    """Firestore REST フィールド値 → Python 値"""
    if "nullValue"    in val: return None
    if "booleanValue" in val: return val["booleanValue"]
    if "integerValue" in val: return int(val["integerValue"])
    if "doubleValue"  in val: return float(val["doubleValue"])
    if "stringValue"  in val: return val["stringValue"]
    if "timestampValue" in val: return val["timestampValue"]
    if "arrayValue"   in val:
        return [_from_fs(v) for v in val["arrayValue"].get("values", [])]
    if "mapValue"     in val:
        return {k: _from_fs(v) for k, v in val["mapValue"].get("fields", {}).items()}
    return None
