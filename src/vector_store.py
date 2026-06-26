"""
ベクトルストア（ChromaDB + Gemini Embeddings）
Firestore に保存された KB ドキュメントを意味検索可能にする。
ローカルの output/chroma/ に永続化する。
"""
import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_PERSIST_DIR = "output/chroma"
_COLLECTION_NAME = "weekly_relay"


def _build_embed_text(doc_id: str, data: dict) -> str:
    """KB ドキュメントから埋め込み用の代表テキストを組み立てる"""
    src = data.get("source_type", "")
    if src == "ticket":
        parts = [
            f"チケット {data.get('issue_key', '')} {data.get('summary', '')}",
            f"ステータス: {data.get('status', '')}  担当: {data.get('assignee', '')}",
            data.get("ai_summary") or (data.get("description") or "")[:600],
        ]
    elif src == "slack":
        parts = [
            f"Slack #{data.get('channel_name', '')}  {data.get('week_label', '')}",
            data.get("ai_summary", ""),
        ]
    elif src == "meeting":
        parts = [
            f"議事録 {data.get('display_name', '')}  {str(data.get('created_date', ''))[:10]}",
            data.get("ai_summary", ""),
        ]
    else:
        parts = [doc_id]
    return "\n".join(p for p in parts if p).strip()


class VectorStore:
    """ChromaDB を使った KB 意味検索ストア"""

    def __init__(self, embed_fn: Callable[[str], list[float]]):
        """
        embed_fn: text -> list[float]  の callable。
                  GeminiClient.embed を渡す。
        """
        import chromadb
        Path(_PERSIST_DIR).mkdir(parents=True, exist_ok=True)
        self._chroma = chromadb.PersistentClient(path=_PERSIST_DIR)
        self._col = self._chroma.get_or_create_collection(
            _COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._embed = embed_fn
        logger.info(f"VectorStore 初期化: {_PERSIST_DIR}  現在 {self._col.count()} 件")

    # ------------------------------------------------------------------ #

    def upsert(self, doc_id: str, data: dict) -> None:
        """ドキュメントを埋め込んでストアに保存（同 ID は上書き）"""
        text = _build_embed_text(doc_id, data)
        if not text:
            return
        try:
            embedding = self._embed(text)
            meta: dict[str, str] = {
                "source_type": data.get("source_type", ""),
                "doc_id": doc_id,
            }
            for key in ("issue_key", "channel_name", "week_label", "display_name"):
                val = data.get(key)
                if val:
                    meta[key] = str(val)
            created = data.get("created_date")
            if created:
                meta["created_date"] = str(created)[:10]
            self._col.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text[:3000]],
                metadatas=[meta],
            )
        except Exception as e:
            logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """クエリに意味的に近い KB ドキュメントを返す"""
        total = self._col.count()
        if total == 0:
            return []
        try:
            embedding = self._embed(query)
            results = self._col.query(
                query_embeddings=[embedding],
                n_results=min(n_results, total),
                include=["documents", "metadatas", "distances"],
            )
            output = []
            for doc_id, doc, meta, dist in zip(
                results["ids"][0],
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                output.append({
                    "doc_id": doc_id,
                    "score": round(1 - dist, 4),   # cosine similarity（1 = 完全一致）
                    "text": doc,
                    "meta": meta,
                })
            return output
        except Exception as e:
            logger.warning(f"VectorStore search 失敗: {e}")
            return []

    def count(self) -> int:
        return self._col.count()
