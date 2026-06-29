"""
ベクトルストア（ChromaDB ローカル実装）

NOTE: セキュリティレビュー対応のため Firestore Vector Search から
      ローカル ChromaDB に一時的に差し戻し。
      Firestore Vector Search への再切り替えは下部のコメントアウトを参照。
"""
import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)

_PERSIST_DIR = "output/chroma"
_COLLECTION_NAME = "wasabi_kb"


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
    """ChromaDB を使ったローカルベクトルストア。

    引数:
        embed_fn        : text -> list[float] の callable（GeminiClient.embed）
        firestore_client: 現在は未使用（Firestore Vector Search 切り替え時に使用）
    """

    def __init__(self, embed_fn: Callable[[str], list[float]], firestore_client=None):
        import chromadb
        from chromadb.utils.embedding_functions import EmbeddingFunction

        self._embed = embed_fn
        os.makedirs(_PERSIST_DIR, exist_ok=True)
        self._chroma = chromadb.PersistentClient(path=_PERSIST_DIR)

        class _GeminiEF(EmbeddingFunction):
            def __call__(self_, texts):
                return [embed_fn(t) for t in texts]

        self._col = self._chroma.get_or_create_collection(
            _COLLECTION_NAME,
            embedding_function=_GeminiEF(),
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"VectorStore 初期化: ChromaDB ローカル ({_PERSIST_DIR}, {self._col.count()} 件)")

    # ------------------------------------------------------------------ #

    def upsert(self, doc_id: str, data: dict) -> None:
        """ドキュメントを埋め込んで ChromaDB に保存（同 ID は上書き）"""
        text = _build_embed_text(doc_id, data)
        if not text:
            return
        try:
            meta = {
                "source_type": str(data.get("source_type", "")),
                "doc_id": doc_id,
            }
            for key in ("issue_key", "channel_name", "week_label", "display_name"):
                val = data.get(key)
                if val:
                    meta[key] = str(val)
            self._col.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[meta],
            )
        except Exception as e:
            logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """クエリに意味的に近い KB ドキュメントを返す。
        戻り値: [{"doc_id": str, "score": float, "text": str, "meta": dict}, ...]
        """
        try:
            total = self._col.count()
            if total == 0:
                return []
            res = self._col.query(
                query_texts=[query],
                n_results=min(n_results, total),
                include=["documents", "metadatas", "distances"],
            )
            output = []
            for doc_text, meta, dist in zip(
                res["documents"][0],
                res["metadatas"][0],
                res["distances"][0],
            ):
                output.append({
                    "doc_id": meta.get("doc_id", ""),
                    "score": round(1.0 - dist, 4),
                    "text": doc_text[:3000],
                    "meta": meta,
                })
            return output
        except Exception as e:
            logger.warning(f"VectorStore search 失敗: {e}")
            return []

    def count(self) -> int:
        """インデックス済みドキュメント数を返す"""
        try:
            return self._col.count()
        except Exception:
            return 0


# ======================================================================
# Firestore Vector Search 実装（セキュリティレビュー対応のためコメントアウト）
# 再有効化する場合:
#   1. 上記 ChromaDB クラスをコメントアウト
#   2. 下記クラスのコメントを外す
#   3. firestore_client.py の upsert_embedding / vector_search / count_embeddings を復元
#   4. main.py の VectorStore(firestore_client=...) 引数を復元
# ======================================================================

# class VectorStore:
#     """Firestore Vector Search を使ったクラウドベクトルストア。"""
#
#     def __init__(self, embed_fn: Callable[[str], list[float]], firestore_client):
#         self._embed = embed_fn
#         self._fs = firestore_client
#         logger.info("VectorStore 初期化: Firestore Vector Search モード")
#
#     def upsert(self, doc_id: str, data: dict) -> None:
#         text = _build_embed_text(doc_id, data)
#         if not text:
#             return
#         try:
#             embedding = self._embed(text)
#             self._fs.upsert_embedding(doc_id, embedding)
#         except Exception as e:
#             logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")
#
#     def search(self, query: str, n_results: int = 5) -> list[dict]:
#         try:
#             embedding = self._embed(query)
#             raw = self._fs.vector_search(embedding, n_results)
#             output = []
#             for item in raw:
#                 data = item.get("data", {})
#                 text = _build_embed_text(item["doc_id"], data)
#                 meta = {"source_type": data.get("source_type", ""), "doc_id": item["doc_id"]}
#                 for key in ("issue_key", "channel_name", "week_label", "display_name"):
#                     val = data.get(key)
#                     if val:
#                         meta[key] = str(val)
#                 output.append({
#                     "doc_id": item["doc_id"],
#                     "score": item["score"],
#                     "text": text[:3000],
#                     "meta": meta,
#                 })
#             return output
#         except Exception as e:
#             logger.warning(f"VectorStore search 失敗: {e}")
#             return []
#
#     def count(self) -> int:
#         try:
#             return self._fs.count_embeddings()
#         except Exception:
#             return 0
