"""
SmartSync Firestore ストア（FirestoreClient 互換）

weekly-relay Firestore の廃止に伴い、週次レポート・手動メモ・共有事項・同期ログの
保存先を SmartSync Firestore（andst-hd-ax）の wasabi_ プレフィックス付きコレクションに移す。

src/firestore_client.py の FirestoreClient と同一インターフェースのため、
main.py 側は import の差し替えのみで移行できる。

コレクション対応:
  weekly_reports   → wasabi_weekly_reports
  calendar_reports → wasabi_calendar_reports
  manual_memos     → wasabi_manual_memos
  shared_infos     → wasabi_shared_infos
  sync_logs        → wasabi_sync_logs
"""
import logging
from datetime import datetime, timezone, timedelta

from src import smartsync_client as sc

logger = logging.getLogger(__name__)

_TTL_DAYS_SYNC_LOG = 90


class SmartSyncStore:
    """Wasabi 用 SmartSync Firestore CRUD ラッパー（FirestoreClient 互換）"""

    # ------------------------------------------------------------------ #
    #  context_snapshots（--only firestore の接続テスト用）
    # ------------------------------------------------------------------ #

    def get_context_snapshot(self, doc_id: str) -> dict | None:
        try:
            return sc.get_context_snapshot(doc_id)
        except Exception as e:
            logger.warning(f"SmartSync get 失敗 ({doc_id}): {e}")
            return None

    def save_context_snapshot(self, doc_id: str, data: dict) -> None:
        try:
            payload = {**data, "saved_at": datetime.now(timezone.utc)}
            sc.save_context_snapshot(doc_id, payload)
        except Exception as e:
            logger.warning(f"SmartSync save 失敗 ({doc_id}): {e}")

    def list_context_snapshots(self, page_size: int = 100) -> list[tuple[str, dict]]:
        try:
            return sc.list_context_snapshots(page_size)
        except Exception as e:
            logger.warning(f"SmartSync list 失敗: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  weekly_reports
    # ------------------------------------------------------------------ #

    def save_weekly_report(self, week_start: datetime, week_end: datetime,
                            content: str) -> None:
        try:
            doc_id = week_start.strftime("%Y%m%d")
            sc.save_doc("wasabi_weekly_reports", doc_id, {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "content": content,
                "saved_at": datetime.now(timezone.utc),
            })
            logger.info(f"週次レポート保存: wasabi_weekly_reports/{doc_id}")
        except Exception as e:
            logger.warning(f"週次レポート保存失敗: {e}")

    # ------------------------------------------------------------------ #
    #  calendar_reports
    # ------------------------------------------------------------------ #

    def save_calendar_report(self, first_event_date: datetime, content: str) -> None:
        try:
            doc_id = first_event_date.strftime("%Y%m%d")
            sc.save_doc("wasabi_calendar_reports", doc_id, {
                "content": content,
                "saved_at": datetime.now(timezone.utc),
            })
            logger.info(f"カレンダーレポート保存: wasabi_calendar_reports/{doc_id}")
        except Exception as e:
            logger.warning(f"カレンダーレポート保存失敗: {e}")

    # ------------------------------------------------------------------ #
    #  manual_memos（Slack Bot 手動メモ）
    # ------------------------------------------------------------------ #

    def save_manual_memo(self, text: str, parent_issue_key: str = "",
                          created_by: str = "", channel: str = "") -> str:
        """手動メモを wasabi_manual_memos に保存してドキュメントIDを返す"""
        try:
            now = datetime.now(timezone.utc)
            ts_ms = int(now.timestamp() * 1000)
            doc_id = f"memo_{now.strftime('%Y%m%d')}_{ts_ms}"
            sc.save_doc("wasabi_manual_memos", doc_id, {
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
            results = []
            for _, data in sc.list_docs("wasabi_manual_memos"):
                created_at = data.get("created_at")
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                if not isinstance(created_at, datetime):
                    continue
                if since.tzinfo and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                elif not since.tzinfo and created_at.tzinfo:
                    created_at = created_at.replace(tzinfo=None)
                if since <= created_at <= until:
                    results.append(data)
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
        """共有事項（Backlog 起票済み）を wasabi_shared_infos に保存する"""
        try:
            now = datetime.now(timezone.utc)
            ts_ms = int(now.timestamp() * 1000)
            doc_id = f"shared_{now.strftime('%Y%m%d')}_{ts_ms}"
            sc.save_doc("wasabi_shared_infos", doc_id, {
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
            sc.add_doc("wasabi_sync_logs", {
                "job": job,
                "status": status,
                "detail": detail,
                "duration_sec": duration_sec,
                "created_at": now,
                "expire_at": now + timedelta(days=_TTL_DAYS_SYNC_LOG),
            })
        except Exception as e:
            logger.warning(f"sync_log 書き込み失敗: {e}")
