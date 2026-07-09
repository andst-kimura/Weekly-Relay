"""
/api/kb — KB（context_snapshots の wasabi_*）の探索 API

- 一覧・全文表示は Firestore の読み取りのみ（全員可）
- 意味検索は Gemini embedding が必要（GEMINI_API_KEY 未設定の環境では 400）
"""
import os
import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException

from app.deps import CurrentUser, get_current_user
from src import smartsync_client as sc

logger = logging.getLogger(__name__)
router = APIRouter()

_TYPES = {"backlog", "slack", "meeting"}


def _summary_row(doc_id: str, data: dict) -> dict:
    """一覧・検索結果用の軽量ドキュメント表現"""
    return {
        "doc_id": doc_id,
        "source_type": data.get("source_type", ""),
        "source_key": data.get("source_key", ""),
        "source_name": data.get("source_name", ""),
        "source_url": data.get("source_url", ""),
        "synced_at": str(data.get("synced_at", ""))[:19],
        "preview": (data.get("ai_text") or "")[:120],
        "has_embedding": "embedding" in data,
    }


@lru_cache(maxsize=1)
def _gemini_embed():
    """Gemini embedding 関数を返す（キー未設定なら None）"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        from src.gemini_client import GeminiClient
        client = GeminiClient(api_key=api_key, model="gemini-2.5-flash")
        return client.embed if client.enabled else None
    except Exception as e:
        logger.warning(f"Gemini 初期化失敗: {e}")
        return None


@router.get("/kb/docs")
async def list_kb_docs(type: str = "", limit: int = 50,
                        user: CurrentUser = Depends(get_current_user)):
    """wasabi_* の KB ドキュメント一覧（synced_at 降順）"""
    try:
        docs = sc.list_context_snapshots()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore 読み取り失敗: {e}")
    rows = [
        _summary_row(doc_id, data)
        for doc_id, data in docs
        if doc_id.startswith("wasabi_")
        and (not type or data.get("source_type") == type)
    ]
    rows.sort(key=lambda r: r["synced_at"], reverse=True)
    return {"docs": rows[: min(limit, 200)], "total": len(rows)}


@router.get("/kb/docs/{doc_id}")
async def get_kb_doc(doc_id: str, user: CurrentUser = Depends(get_current_user)):
    """KB ドキュメント1件の全文"""
    if not doc_id.startswith("wasabi_"):
        raise HTTPException(status_code=403, detail="wasabi_* ドキュメントのみ閲覧できます")
    data = sc.get_context_snapshot(doc_id)
    if not data:
        raise HTTPException(status_code=404, detail="Document not found")
    row = _summary_row(doc_id, data)
    row["ai_text"] = data.get("ai_text", "")
    return row


@router.get("/kb/search")
async def search_kb(q: str, type: str = "",
                     user: CurrentUser = Depends(get_current_user)):
    """意味検索（Gemini embedding + Firestore Vector Search）"""
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="検索キーワードを入力してください")
    embed = _gemini_embed()
    if embed is None:
        raise HTTPException(
            status_code=400,
            detail="この環境では意味検索が使えません（GEMINI_API_KEY 未設定）。一覧から探してください")
    if type and type not in _TYPES:
        raise HTTPException(status_code=400, detail=f"type は {sorted(_TYPES)} のいずれか")
    try:
        embedding = embed(q)
        raw = sc.vector_search(embedding, n_results=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"検索失敗: {e}")
    results = []
    for item in raw:
        doc_id = item["doc_id"]
        data = item.get("data", {})
        if not doc_id.startswith("wasabi_"):
            continue
        if type and data.get("source_type") != type:
            continue
        row = _summary_row(doc_id, data)
        row["score"] = item.get("score", 0.0)
        results.append(row)
    return {"results": results[:10], "query": q}
