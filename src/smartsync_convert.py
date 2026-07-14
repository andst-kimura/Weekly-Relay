"""
Wasabi 形式 → SmartSync 形式の変換ロジック

knowledge_base.py（週次 KB 生成）と scripts/migrate_to_smartsync.py（過去データ移行）の
両方から使う共通モジュール。

SmartSync の context_snapshots スキーマ:
  doc_id       : {project_id}_{source_type}_{source_key}
  project_id   : プロジェクトID（Wasabi は "wasabi_sales" 固定）
  source_type  : "backlog" / "slack" / "meeting"
  source_key   : Backlog課題キー / SlackチャンネルID / 議事録スラグ
  source_name  : 表示名
  ai_text      : Gemini 用フォーマット済みテキスト
  knowledge_text: NotebookLM 用テキスト
  synced_at    : 同期日時
"""
import re
from datetime import datetime, timezone

# Wasabi データの SmartSync 上での project_id
WASABI_PROJECT_ID = "wasabi_sales"


def _slug(text: str, max_len: int = 40) -> str:
    """テキストを doc_id に使えるスラグに変換"""
    s = re.sub(r"[^\w぀-ヿ一-鿿]", "_", text or "")
    return s[:max_len].strip("_")


def _comments_to_text(comments: list) -> str:
    """Wasabi の comments 配列をテキストに整形"""
    if not comments:
        return ""
    lines = []
    for c in comments:
        dt = (c.get("created") or "")[:16]
        user = c.get("user", "不明")
        content = (c.get("content") or "").strip()
        if content:
            lines.append(f"[{dt}] {user}: {content}")
    return "\n".join(lines)


def _threads_to_text(standalone: list, threads: list) -> str:
    """Wasabi の standalone_messages + threads をテキストに整形"""
    lines = []
    for msg in (standalone or []):
        dt = (msg.get("datetime") or "")[:16]
        user = msg.get("user_name", "自分")
        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f"[{dt}] {user}: {text}")
    for thread in (threads or []):
        for msg in thread.get("messages", []):
            dt = (msg.get("datetime") or "")[:16]
            user = msg.get("user", "不明")
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"[{dt}] {user}: {text}")
    return "\n".join(lines)


def convert(wasabi_doc_id: str, wasabi_data: dict) -> tuple[str, dict] | None:
    """
    Wasabi フォーマット → SmartSync フォーマットに変換。
    変換不可（未知の source_type 等）の場合は None を返す。
    """
    source_type = wasabi_data.get("source_type", "")

    # ----- チケット (ticket → backlog) -----
    if source_type == "ticket":
        issue_key = wasabi_data.get("issue_key", "")
        if not issue_key:
            return None
        source_key = issue_key  # 例: SALES_TEAM-27
        source_name = wasabi_data.get("summary", issue_key)[:100]
        doc_id = f"{WASABI_PROJECT_ID}_backlog_{source_key}"

        ai_summary = wasabi_data.get("ai_summary", "")
        comments_text = _comments_to_text(wasabi_data.get("comments", []))
        ai_text_parts = []
        if ai_summary:
            ai_text_parts.append(f"【AIサマリー】\n{ai_summary}")
        status = wasabi_data.get("status", "")
        assignee = wasabi_data.get("assignee", "")
        description = (wasabi_data.get("description") or "")[:500]
        info_lines = []
        if status:
            info_lines.append(f"ステータス: {status}")
        if assignee:
            info_lines.append(f"担当者: {assignee}")
        if description:
            info_lines.append(f"説明: {description}")
        if info_lines:
            ai_text_parts.append("\n".join(info_lines))
        if comments_text:
            ai_text_parts.append(f"【コメント履歴】\n{comments_text}")

        sm_data = {
            "doc_id": doc_id,
            "project_id": WASABI_PROJECT_ID,
            "source_type": "backlog",
            "source_key": source_key,
            "source_name": source_name,
            "ai_text": "\n\n".join(ai_text_parts),
            "knowledge_text": "\n\n".join(ai_text_parts),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        # 差分スキップ判定用（Wasabi 独自フィールド。SmartSync 側は無視する）
        backlog_updated_at = wasabi_data.get("backlog_updated_at", "")
        if backlog_updated_at:
            sm_data["backlog_updated_at"] = backlog_updated_at
        # 人ナビ（詳しい人の推定）用に担当者を構造化保存
        if assignee and assignee != "未設定":
            sm_data["assignee"] = assignee
        return doc_id, sm_data

    # ----- Slack (slack → slack) -----
    elif source_type == "slack":
        channel_id = wasabi_data.get("channel_id", "")
        channel_name = wasabi_data.get("channel_name", "")
        source_key = channel_id or _slug(channel_name)
        if not source_key:
            return None
        week_label = wasabi_data.get("week_label", "")
        doc_id = f"{WASABI_PROJECT_ID}_slack_{source_key}"

        ai_summary = wasabi_data.get("ai_summary", "")
        messages_text = _threads_to_text(
            wasabi_data.get("standalone_messages", []),
            wasabi_data.get("threads", []),
        )
        ai_text_parts = []
        if week_label:
            ai_text_parts.append(f"期間: {week_label}")
        if ai_summary:
            ai_text_parts.append(f"【AIサマリー】\n{ai_summary}")
        if messages_text:
            ai_text_parts.append(f"【メッセージ】\n{messages_text}")

        sm_data = {
            "doc_id": doc_id,
            "project_id": WASABI_PROJECT_ID,
            "source_type": "slack",
            "source_key": source_key,
            "source_name": channel_name or channel_id,
            "ai_text": "\n\n".join(ai_text_parts),
            "knowledge_text": "\n\n".join(ai_text_parts),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        return doc_id, sm_data

    # ----- 議事録 (meeting → meeting) -----
    elif source_type == "meeting":
        display_name = wasabi_data.get("display_name") or wasabi_data.get("title", "")
        created_date = wasabi_data.get("created_date", "")
        # doc_id の source_key は元の doc_id から生成（再現性を確保）
        source_key = _slug(wasabi_doc_id, 60)
        doc_id = f"{WASABI_PROJECT_ID}_meeting_{source_key}"

        ai_summary = wasabi_data.get("ai_summary", "")
        raw_text = (wasabi_data.get("raw_text") or "")[:1000]
        ai_text_parts = []
        if created_date:
            ai_text_parts.append(f"日付: {created_date[:10]}")
        if ai_summary:
            ai_text_parts.append(f"【AIサマリー】\n{ai_summary}")
        if raw_text:
            ai_text_parts.append(f"【議事録テキスト（抜粋）】\n{raw_text}")

        sm_data = {
            "doc_id": doc_id,
            "project_id": WASABI_PROJECT_ID,
            "source_type": "meeting",
            "source_key": source_key,
            "source_name": display_name[:100] if display_name else "議事録",
            "ai_text": "\n\n".join(ai_text_parts),
            "knowledge_text": "\n\n".join(ai_text_parts),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        # Google Docs へのリンク（出典表示用）
        google_doc_id = wasabi_data.get("google_doc_id", "")
        if google_doc_id:
            sm_data["source_url"] = f"https://docs.google.com/document/d/{google_doc_id}/edit"
        return doc_id, sm_data

    else:
        return None
