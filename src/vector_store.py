"""
ベクトルストア（Firestore Vector Search + Gemini Embeddings）
ChromaDB を廃止し、Firestore の findNearest を使ってクラウド上で意味検索を行う。
embedding フィールドは context_snapshots コレクションの各ドキュメントに追記される。

事前準備: Firestore ベクトルインデックスの作成が必要。
  infra/create_vector_index.sh を実行してください。
"""
import logging
from typing import Callable

logger = logging.getLogger(__name__)


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
    """Firestore Vector Search を使った KB 意味検索ストア。
    ローカルファイルは一切使用しない。
    """

    def __init__(self, embed_fn: Callable[[str], list[float]], firestore_client):
        """
        embed_fn        : text -> list[float] の callable（GeminiClient.embed）
        firestore_client: FirestoreClient インスタンス
        """
        self._embed = embed_fn
        self._fs = firestore_client
        logger.info("VectorStore 初期化: Firestore Vector Search モード")

    # ------------------------------------------------------------------ #

    def upsert(self, doc_id: str, data: dict) -> None:
        """ドキュメントを埋め込んで Firestore に保存（同 ID は上書き）"""
        text = _build_embed_text(doc_id, data)
        if not text:
            return
        try:
            embedding = self._embed(text)
            self._fs.upsert_embedding(doc_id, embedding)
        except Exception as e:
            logger.warning(f"VectorStore upsert 失敗 ({doc_id}): {e}")

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """クエリに意味的に近い KB ドキュメントを返す。
        戻り値: [{"doc_id": str, "score": float, "text": str, "meta": dict}, ...]
        """
        try:
            embedding = self._embed(query)
            raw = self._fs.vector_search(embedding, n_results)
            output = []
            for item in raw:
                data = item.get("data", {})
                # 表示用テキストを data から再構築
                text = _build_embed_text(item["doc_id"], data)
                meta = {
                    "source_type": data.get("source_type", ""),
                    "doc_id": item["doc_id"],
                }
                for key in ("issue_key", "channel_name", "week_label", "display_name"):
                    val = data.get(key)
                    if val:
                        meta[key] = str(val)
                output.append({
                    "doc_id": item["doc_id"],
                    "score": item["score"],
                    "text": text[:3000],
                    "meta": meta,
                })
            return output
        except Exception as e:
            logger.warning(f"VectorStore search 失敗: {e}")
            return []

    def count(self) -> int:
        """embedding フィールドを持つドキュメント数を返す"""
        try:
            return self._fs.count_embeddings()
        except Exception:
            return 0
