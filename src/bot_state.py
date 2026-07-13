"""
Slack Bot の状態永続化（Firestore）

Bot がメモリに持っていた状態（転記プレビュー・二重実行ガード・完了待ち・削除用 ts）を
Firestore に移し、再起動・マルチインスタンス（Events API 化）に耐える構造にする。

コレクション:
  wasabi_pending_posts/{pending_id} … 転記プレビューのメタ（1時間で無効扱い）
  wasabi_job_status/lock            … 実行中ジョブのロック + 完了待ちリスト
  wasabi_reply_ts/{key}             … Bot 返信の削除用 ts
"""
import logging
from datetime import datetime, timezone, timedelta

from src import smartsync_client as sc

logger = logging.getLogger(__name__)

_PENDING_COLLECTION = "wasabi_pending_posts"
_LOCK_COLLECTION = "wasabi_job_status"
_LOCK_DOC = "lock"
_REPLY_COLLECTION = "wasabi_reply_ts"

PENDING_TTL_MINUTES = 60
LOCK_TTL_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(val) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _get_doc(collection: str, doc_id: str) -> dict | None:
    url = sc._doc_url(collection, doc_id)
    resp = sc._get_session().get(url, timeout=15)
    if resp.status_code != 200:
        return None
    return sc._parse_doc(resp.json())


# --------------------------------------------------------------------------- #
#  転記プレビューの保留（pending posts）
# --------------------------------------------------------------------------- #

def save_pending_post(pending_id: str, meta: dict) -> None:
    """転記プレビューのメタ（期間等）を保存する。収集データ本体は保存しない。"""
    sc.save_doc(_PENDING_COLLECTION, pending_id, {**meta, "created_at": _now()})


def pop_pending_post(pending_id: str) -> dict | None:
    """プレビューのメタを取得して削除する。期限切れ（1時間超）は None。"""
    data = _get_doc(_PENDING_COLLECTION, pending_id)
    sc.delete_doc(_PENDING_COLLECTION, pending_id)
    if not data:
        return None
    created = _parse_ts(data.get("created_at"))
    if not created or _now() - created > timedelta(minutes=PENDING_TTL_MINUTES):
        return None
    return data


# --------------------------------------------------------------------------- #
#  ジョブロック（二重実行ガード）+ 完了待ちリスト
# --------------------------------------------------------------------------- #

def acquire_job_lock(job: str, holder: str = "") -> bool:
    """ジョブロックを取得する。実行中（30分以内に取得されたロックあり）なら False。

    NOTE: read-then-write のため厳密なアトミック性はないが、
    人間のコマンド操作（秒単位の間隔）の二重実行防止としては十分。
    """
    data = _get_doc(_LOCK_COLLECTION, _LOCK_DOC)
    if data and data.get("running"):
        started = _parse_ts(data.get("started_at"))
        if started and _now() - started < timedelta(minutes=LOCK_TTL_MINUTES):
            return False
        logger.warning(f"古いジョブロック（{started}）を上書きします")
    sc.save_doc(_LOCK_COLLECTION, _LOCK_DOC, {
        "running": True,
        "job": job,
        "holder": holder,
        "started_at": _now(),
        "waiters": [],
    })
    return True


def release_job_lock() -> None:
    try:
        sc.save_doc(_LOCK_COLLECTION, _LOCK_DOC, {"running": False, "waiters": []})
    except Exception as e:
        logger.warning(f"ジョブロック解放失敗: {e}")


def add_job_waiter(channel: str, thread_ts: str = "") -> None:
    """実行中ジョブの完了通知先を追加する"""
    data = _get_doc(_LOCK_COLLECTION, _LOCK_DOC) or {}
    waiters = data.get("waiters") or []
    waiters.append({"channel": channel, "thread_ts": thread_ts})
    sc.save_doc(_LOCK_COLLECTION, _LOCK_DOC, {**data, "waiters": waiters})


def pop_job_waiters() -> list[dict]:
    """完了待ちリストを取得してクリアする"""
    data = _get_doc(_LOCK_COLLECTION, _LOCK_DOC) or {}
    waiters = data.get("waiters") or []
    if waiters:
        sc.save_doc(_LOCK_COLLECTION, _LOCK_DOC, {**data, "waiters": []})
    return [w for w in waiters if isinstance(w, dict) and w.get("channel")]


# --------------------------------------------------------------------------- #
#  Bot 返信の削除用 ts
# --------------------------------------------------------------------------- #

def save_reply_ts(key: str, ts: str) -> None:
    try:
        sc.save_doc(_REPLY_COLLECTION, key, {"ts": ts, "created_at": _now()})
    except Exception as e:
        logger.warning(f"reply_ts 保存失敗: {e}")


def pop_reply_ts(key: str) -> str | None:
    data = _get_doc(_REPLY_COLLECTION, key)
    if not data:
        return None
    sc.delete_doc(_REPLY_COLLECTION, key)
    return data.get("ts")
