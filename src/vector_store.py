"""
ベクトルストア（SmartSync Firestore Vector Search 実装）

SmartSync の context_snapshots コレクションに embedding フィールドを追記し、
findNearest クエリで意味検索を行う。
"""
import logging
import os
import re
from typing import Callable

logger = logging.getLogger(__name__)

# 課題キーのパターン（ハイブリッド検索の直接ヒット用）
_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")


def _build_embed_text(doc_id: str, data: dict) -> str:
    """KB ドキュメントから埋め込み用の代表テキストを組み立てる"""
    src = data.get("source_type", "")
    if src == "backlog":
        parts = [
            f"チケット {data.get('source_key', '')} {data.get('source_name', '')}",
            data.get("ai_text") or data.get("knowledge_text") or "",
        ]
    elif src == "slack":
        parts = [
            f"Slack #{data.get('source_name', '')}",
            data.get("ai_text", ""),
        ]
    elif src in ("meeting", "meet_notes"):
        parts = [
            f"議事録 {data.get('source_name', '')}",
            data.get("ai_text", ""),
        ]
    else:
        # Wasabi 独自形式（ticket / slack / meeting）の互換サポート
        if src == "ticket":
            parts = [
                f"チケット {data.get('issue_key', '')} {data.get('summary', '')}",
                data.get("ai_summary") or (data.get("description") or "")[:600],
            ]
        elif src == "slack":
            parts = [
                f"Slack #{data.get('channel_name', '')}  {data.get('week_label', '')}",
                data.get("ai_summary", ""),
            ]
        elif src == "meeting":
            parts = [
                f"議事録 {data.get('display_name', '')}",
                data.get("ai_summary", ""),
            ]
        else:
            parts = [doc_id]
    return "\n".join(p for p in parts if p).strip()


class VectorStore:
    """SmartSync Firestore Vector Search を使ったベクトルストア。

    引数:
        embed_fn: text -> list[float] の callable（GeminiClient.embed）
    """

    def __init__(self, embed_fn: Callable[[str], list[float]], firestore_client=None):
        self._embed = embed_fn
        from src import smartsync_client as _sc
        self._sc = _sc
        count = _sc.count_with_embedding()
        logger.info(
            f"VectorStore 初期化: SmartSync Firestore Vector Search "
            f"（{os.environ.get('SMARTSYNC_FIRESTORE_DATABASE', 'smart-sync-stg')}）"
            f" embedding 済み {count} 件"
        )

    def upsert(self, doc_id: str, data: dict) -> None:
        """テキストを embedding 化して SmartSync Firestore に書き込む。"""
        text = _build_embed_text(doc_id, data)
        if not text:
            return
        try:
            embedding = self._embed(text)
            self._sc.write_embedding(doc_id, embedding)
        except Exception as e:
            logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")

    @staticmethod
    def _to_result(doc_id: str, data: dict, score: float) -> dict:
        """検索結果1件の共通フォーマットを組み立てる"""
        text = _build_embed_text(doc_id, data)
        meta = {
            "source_type": data.get("source_type", ""),
            "doc_id": doc_id,
        }
        for key in ("source_key", "source_name", "project_id",
                    "issue_key", "channel_name", "week_label", "display_name"):
            val = data.get(key)
            if val:
                meta[key] = str(val)
        return {"doc_id": doc_id, "score": score, "text": text[:3000], "meta": meta}

    def _keyword_hits(self, query: str) -> list[dict]:
        """クエリ中の課題キーに対応する KB ドキュメントを直接取得する（ハイブリッド検索）。

        ベクトル検索は固有 ID の一致を保証しないため、
        課題キーが明示された質問では該当ドキュメントを必ず結果に含める。
        """
        issue_keys = _ISSUE_KEY_RE.findall(query)[:3]  # 1クエリ最大3キー
        if not issue_keys:
            return []
        # 登録チームの project_id で doc_id を組み立てて直接 GET
        try:
            from src.team_config import list_teams
            team_ids = [t["team_id"] for t in list_teams()] or ["sales"]
        except Exception:
            team_ids = ["sales"]
        hits = []
        for key in issue_keys:
            for tid in team_ids:
                doc_id = f"wasabi_{tid}_backlog_{key}"
                try:
                    data = self._sc.get_context_snapshot(doc_id)
                except Exception:
                    data = None
                if data:
                    hits.append(self._to_result(doc_id, data, score=1.0))
                    break  # 同一キーは最初に見つかったチームのもののみ
        if hits:
            logger.info(f"ハイブリッド検索: 課題キー直接ヒット {[h['doc_id'] for h in hits]}")
        return hits

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """クエリに意味的に近い KB ドキュメントを返す（課題キーは直接取得を併用）。
        戻り値: [{"doc_id": str, "score": float, "text": str, "meta": dict}, ...]
        """
        try:
            # ① キーワード（課題キー）直接ヒット
            keyword_hits = self._keyword_hits(query)
            seen = {h["doc_id"] for h in keyword_hits}

            # ② ベクトル検索
            embedding = self._embed(query)
            raw = self._sc.vector_search(embedding, n_results)
            vector_hits = [
                self._to_result(item["doc_id"], item.get("data", {}), item["score"])
                for item in raw
                if item["doc_id"] not in seen
            ]

            # 直接ヒットを先頭に、全体で n_results + キーワード分まで
            return (keyword_hits + vector_hits)[: n_results + len(keyword_hits)]
        except Exception as e:
            logger.warning(f"VectorStore search 失敗: {e}")
            return []

    def count(self) -> int:
        try:
            return self._sc.count_with_embedding()
        except Exception:
            return 0


# ======================================================================
# ChromaDB 実装（ローカル）
# 再有効化する場合:
#   1. 上記 Firestore Vector Search クラスをコメントアウト
#   2. 下記クラスのコメントを外す
# ======================================================================

# _PERSIST_DIR     = "output/chroma"
# _COLLECTION_NAME = "wasabi_kb"
#
# class VectorStore:
#     def __init__(self, embed_fn, firestore_client=None):
#         import chromadb
#         from chromadb.utils.embedding_functions import EmbeddingFunction
#         self._embed = embed_fn
#         os.makedirs(_PERSIST_DIR, exist_ok=True)
#         self._chroma = chromadb.PersistentClient(path=_PERSIST_DIR)
#         class _GeminiEF(EmbeddingFunction):
#             def __call__(self_, texts):
#                 return [embed_fn(t) for t in texts]
#         self._col = self._chroma.get_or_create_collection(
#             _COLLECTION_NAME, embedding_function=_GeminiEF(),
#             metadata={"hnsw:space": "cosine"})
#         logger.info(f"VectorStore 初期化: ChromaDB ({_PERSIST_DIR}, {self._col.count()} 件)")
#
#     def upsert(self, doc_id, data):
#         text = _build_embed_text(doc_id, data)
#         if not text: return
#         try:
#             meta = {"source_type": str(data.get("source_type", "")), "doc_id": doc_id}
#             for key in ("issue_key", "channel_name", "week_label", "display_name"):
#                 val = data.get(key)
#                 if val: meta[key] = str(val)
#             self._col.upsert(ids=[doc_id], documents=[text], metadatas=[meta])
#         except Exception as e:
#             logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")
#
#     def search(self, query, n_results=5):
#         try:
#             total = self._col.count()
#             if total == 0: return []
#             res = self._col.query(
#                 query_texts=[query], n_results=min(n_results, total),
#                 include=["documents", "metadatas", "distances"])
#             return [{"doc_id": m.get("doc_id",""), "score": round(1.0-d,4),
#                      "text": t[:3000], "meta": m}
#                     for t, m, d in zip(
#                         res["documents"][0], res["metadatas"][0], res["distances"][0])]
#         except Exception as e:
#             logger.warning(f"VectorStore search 失敗: {e}")
#             return []
#
#     def count(self):
#         try: return self._col.count()
#         except: return 0
