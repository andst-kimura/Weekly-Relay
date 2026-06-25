"""
Firestore クライアント
個人用 GCP プロジェクト（weekly-relay）の Firestore DB に接続する。
接続はシングルトン（@lru_cache）で初期化を1回に抑える。
"""
import os
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from google.cloud import firestore

logger = logging.getLogger(__name__)

_TTL_DAYS_SYNC_LOG = 90
_TTL_DAYS_CONTEXT = 30


@lru_cache(maxsize=1)
def _get_db() -> firestore.Client:
    db_name = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
    # 社内プロキシ環境では gRPC の SSL ハンドシェイクが失敗するため REST トランスポートに切り替え
    # prefer_rest は >= 2.14.0 で利用可能。古いバージョンは REST transport を直接指定。
    try:
        return firestore.Client(database=db_name, project=project, prefer_rest=True)
    except TypeError:
        pass
    try:
        from google.cloud.firestore_v1.services.firestore.transports.rest import (
            FirestoreRestTransport,
        )
        transport = FirestoreRestTransport(host="firestore.googleapis.com")
        return firestore.Client(database=db_name, project=project, _transport=transport)
    except Exception as e:
        logger.warning(f"REST transport 設定失敗、gRPC にフォールバック: {e}")
        return firestore.Client(database=db_name, project=project)


class FirestoreClient:
    """Weekly Relay 用 Firestore CRUD ラッパー"""

    # ------------------------------------------------------------------ #
    #  context_snapshots（チケット / Slack / 議事録 KB）
    # ------------------------------------------------------------------ #

    def get_context_snapshot(self, doc_id: str) -> dict | None:
        """スナップショットを取得。存在しない場合は None を返す。"""
        try:
            doc = _get_db().collection("context_snapshots").document(doc_id).get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            logger.warning(f"Firestore get 失敗 ({doc_id}): {e}")
            return None

    def save_context_snapshot(self, doc_id: str, data: dict) -> None:
        """スナップショットを upsert する。expire_at で TTL を設定。"""
        try:
            now = datetime.now(timezone.utc)
            payload = {
                **data,
                "saved_at": now,
                "expire_at": now + timedelta(days=_TTL_DAYS_CONTEXT),
            }
            _get_db().collection("context_snapshots").document(doc_id).set(payload)
        except Exception as e:
            logger.warning(f"Firestore save 失敗 ({doc_id}): {e}")

    # ------------------------------------------------------------------ #
    #  weekly_reports
    # ------------------------------------------------------------------ #

    def save_weekly_report(self, week_start: datetime, week_end: datetime,
                            content: str) -> None:
        """週次レポートを保存する。ドキュメント ID は週開始日（YYYYMMDD）。"""
        try:
            doc_id = week_start.strftime("%Y%m%d")
            _get_db().collection("weekly_reports").document(doc_id).set({
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
        """カレンダー工数レポートを保存する。"""
        try:
            doc_id = first_event_date.strftime("%Y%m%d")
            _get_db().collection("calendar_reports").document(doc_id).set({
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
        """実行ログを追記する。TTL 90日で自動削除。"""
        try:
            now = datetime.now(timezone.utc)
            _get_db().collection("sync_logs").add({
                "job": job,
                "status": status,
                "detail": detail,
                "duration_sec": duration_sec,
                "created_at": now,
                "expire_at": now + timedelta(days=_TTL_DAYS_SYNC_LOG),
            })
        except Exception as e:
            logger.warning(f"Firestore sync_log 書き込み失敗: {e}")
