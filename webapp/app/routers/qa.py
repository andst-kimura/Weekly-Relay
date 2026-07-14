"""
/api/qa — Bot 利用状況（質問ログ・フィードバック）の集計 API
"""
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.deps import CurrentUser, get_current_user
from src import smartsync_client as sc

logger = logging.getLogger(__name__)
router = APIRouter()


def _load_logs(days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for doc_id, data in sc.list_docs("wasabi_qa_logs"):
        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(created, datetime):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < since:
            continue
        data["doc_id"] = doc_id
        data["created_at"] = created.isoformat()
        rows.append(data)
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


@router.get("/qa/stats")
async def qa_stats(days: int = 30, user: CurrentUser = Depends(get_current_user)):
    try:
        rows = _load_logs(days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"読み取り失敗: {e}")
    total = len(rows)
    unanswered = sum(1 for r in rows if not r.get("answered"))
    good = sum(1 for r in rows if r.get("feedback") == "good")
    bad = sum(1 for r in rows if r.get("feedback") == "bad")
    by_day: dict[str, int] = {}
    for r in rows:
        d = r["created_at"][:10]
        by_day[d] = by_day.get(d, 0) + 1
    return {
        "days": days,
        "total": total,
        "unanswered": unanswered,
        "unanswered_rate": round(unanswered / total * 100, 1) if total else 0.0,
        "feedback_good": good,
        "feedback_bad": bad,
        "by_day": dict(sorted(by_day.items())),
    }


@router.get("/qa/logs")
async def qa_logs(filter: str = "", days: int = 30, limit: int = 50,
                   user: CurrentUser = Depends(get_current_user)):
    try:
        rows = _load_logs(days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"読み取り失敗: {e}")
    if filter == "unanswered":
        rows = [r for r in rows if not r.get("answered")]
    elif filter == "bad":
        rows = [r for r in rows if r.get("feedback") == "bad"]
    out = [{
        "doc_id": r["doc_id"],
        "question": r.get("question", ""),
        "answered": r.get("answered", False),
        "feedback": r.get("feedback", ""),
        "used_history": r.get("used_history", False),
        "is_dm": r.get("is_dm", False),
        "created_at": r["created_at"][:19],
    } for r in rows[:min(limit, 200)]]
    return {"logs": out, "total": len(rows)}
