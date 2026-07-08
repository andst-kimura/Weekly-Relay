"""
/api/teams — チームプロファイル CRUD + マッピング編集 + Backlog 検証 + KB 統計
"""
import os
import re
import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException

from app.deps import CurrentUser, get_current_user, require_admin
from src import smartsync_client as sc
from src import team_config

logger = logging.getLogger(__name__)
router = APIRouter()

_TEAM_ID_RE = re.compile(r"^[a-z0-9_]{2,30}$")
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")

# 更新可能フィールド（ホワイトリスト）
_ALLOWED_KEYS = {
    "team_name", "active", "transfer_enabled", "transfer_mode", "report_project_key",
    "channel_mapping", "members", "admin_slack_ids",
    "google_meet_folder_id", "exclude_projects", "shared_info",
}

# 転記方式（per_issue_project はロジック未実装のため選択不可）
_TRANSFER_MODES = {"single_project"}


@lru_cache(maxsize=1)
def _backlog_client():
    from src.backlog_client import BacklogClient
    return BacklogClient(
        base_url=os.environ.get("BACKLOG_BASE_URL", "https://adastria.backlog.jp"),
        api_key=os.environ.get("BACKLOG_API_KEY", ""),
        my_user_id=0,
    )


# --------------------------------------------------------------------------- #
#  チーム CRUD
# --------------------------------------------------------------------------- #

@router.get("/teams")
async def list_teams(user: CurrentUser = Depends(get_current_user)):
    return {"teams": team_config.list_teams(), "user": user.email, "is_admin": user.is_admin}


@router.get("/teams/{team_id}")
async def get_team(team_id: str, user: CurrentUser = Depends(get_current_user)):
    team = team_config.load_team(team_id)
    if not team.get("team_name"):
        raise HTTPException(status_code=404, detail="Team not found")
    return team


@router.post("/teams")
async def create_team(data: dict, user: CurrentUser = Depends(require_admin)):
    team_id = (data.get("team_id") or "").strip()
    team_name = (data.get("team_name") or "").strip()
    if not _TEAM_ID_RE.match(team_id):
        raise HTTPException(status_code=400, detail="team_id は英小文字・数字・_ の2〜30文字")
    if not team_name:
        raise HTTPException(status_code=400, detail="team_name は必須")
    existing = team_config.load_team(team_id)
    if existing.get("team_name"):
        raise HTTPException(status_code=409, detail="同じ team_id が既に存在します")

    profile = {
        "team_name": team_name,
        "active": True,
        "transfer_enabled": False,   # 転記はデフォルト無効（設定完了後に有効化）
        "transfer_mode": "single_project",
        "report_project_key": "",
        "channel_mapping": {},
        "members": [],
        "admin_slack_ids": [],
        "google_meet_folder_id": "",
        "exclude_projects": [],
        "shared_info": {},
    }
    team_config.save_team(team_id, profile, updated_by=user.email)
    team_config.write_audit(user.email, "create_team", team_id)
    return {"ok": True, "team_id": team_id}


@router.put("/teams/{team_id}")
async def update_team(team_id: str, data: dict, user: CurrentUser = Depends(require_admin)):
    team = team_config.load_team(team_id)
    if not team.get("team_name"):
        raise HTTPException(status_code=404, detail="Team not found")

    update = {k: v for k, v in data.items() if k in _ALLOWED_KEYS}
    if not update:
        raise HTTPException(status_code=400, detail=f"更新可能フィールド: {sorted(_ALLOWED_KEYS)}")

    if "transfer_mode" in update and update["transfer_mode"] not in _TRANSFER_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"transfer_mode は {sorted(_TRANSFER_MODES)} のみ対応（per_issue_project は準備中）")

    # 転記有効化時は転記先の存在を検証
    if update.get("transfer_enabled") and (update.get("report_project_key") or team.get("report_project_key")):
        key = update.get("report_project_key") or team.get("report_project_key")
        try:
            _backlog_client().get_project(key)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Backlog プロジェクト {key} が見つかりません")

    merged = {**{k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")}, **update}
    team_config.save_team(team_id, merged, updated_by=user.email)
    team_config.write_audit(user.email, "update_team", team_id, {"fields": sorted(update.keys())})
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  チャンネルマッピング
# --------------------------------------------------------------------------- #

@router.post("/teams/{team_id}/mappings")
async def add_mapping(team_id: str, data: dict, user: CurrentUser = Depends(require_admin)):
    team = team_config.load_team(team_id)
    if not team.get("team_name"):
        raise HTTPException(status_code=404, detail="Team not found")

    channel_name = (data.get("channel_name") or "").strip().lstrip("#")
    if not channel_name:
        raise HTTPException(status_code=400, detail="channel_name は必須")

    issue_key = (data.get("parent_issue_key") or "").strip()
    label = (data.get("label") or "").strip()

    # 課題キーが指定されていれば Backlog で実在検証 + 課題名を label に自動設定
    if issue_key:
        if not _ISSUE_KEY_RE.match(issue_key):
            raise HTTPException(status_code=400, detail="課題キーの形式が不正です（例: SALES_TEAM-27）")
        try:
            issue = _backlog_client().get_issue(issue_key)
            if not label:
                label = issue.get("summary", "")[:60]
        except Exception:
            raise HTTPException(status_code=400, detail=f"Backlog 課題 {issue_key} が見つかりません")

    mapping = dict(team.get("channel_mapping") or {})
    existing = mapping.get(channel_name) or {}
    # 既存マッピングとマージ（編集時に未送信フィールドを消さない）
    mapping[channel_name] = {
        "channel_id": (data.get("channel_id") or existing.get("channel_id") or "").strip(),
        "parent_issue_key": issue_key,
        "label": label or existing.get("label", ""),
        "project_key": (data.get("project_key") or existing.get("project_key") or "").strip(),
        "related_meeting_keywords": (
            data.get("related_meeting_keywords")
            if data.get("related_meeting_keywords") is not None
            else existing.get("related_meeting_keywords") or []
        ),
    }

    merged = {**{k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")},
              "channel_mapping": mapping}
    team_config.save_team(team_id, merged, updated_by=user.email)
    team_config.write_audit(user.email, "add_mapping", team_id,
                            {"channel": channel_name, "issue": issue_key})
    return {"ok": True, "label": label}


@router.delete("/teams/{team_id}/mappings/{channel_name}")
async def delete_mapping(team_id: str, channel_name: str,
                          user: CurrentUser = Depends(require_admin)):
    team = team_config.load_team(team_id)
    mapping = dict(team.get("channel_mapping") or {})
    if channel_name not in mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    del mapping[channel_name]

    merged = {**{k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")},
              "channel_mapping": mapping}
    team_config.save_team(team_id, merged, updated_by=user.email)
    team_config.write_audit(user.email, "delete_mapping", team_id, {"channel": channel_name})
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  検証・統計
# --------------------------------------------------------------------------- #

@router.get("/teams/{team_id}/backlog-users")
async def search_backlog_users(team_id: str, q: str = "",
                                user: CurrentUser = Depends(get_current_user)):
    """転記先プロジェクトの参加ユーザーから名前で検索して数値 ID を返す"""
    team = team_config.load_team(team_id)
    project_key = team.get("report_project_key") or ""
    if not project_key:
        raise HTTPException(status_code=400,
                            detail="先に転記先 Backlog プロジェクトキーを設定してください")
    try:
        users = _backlog_client().get_project_users(project_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Backlog ユーザー取得失敗: {e}")

    q_lower = q.strip().lower()
    results = [
        {"id": u.get("id"), "name": u.get("name", ""), "user_id": u.get("userId", "")}
        for u in users
        if not q_lower
        or q_lower in (u.get("name") or "").lower()
        or q_lower in (u.get("userId") or "").lower()
    ]
    return {"users": results[:20], "project_key": project_key}


@router.get("/validate/issue/{issue_key}")
async def validate_issue(issue_key: str, user: CurrentUser = Depends(get_current_user)):
    if not _ISSUE_KEY_RE.match(issue_key):
        return {"valid": False, "reason": "形式が不正です"}
    try:
        issue = _backlog_client().get_issue(issue_key)
        return {"valid": True, "summary": issue.get("summary", "")}
    except Exception:
        return {"valid": False, "reason": "課題が見つかりません"}


@router.get("/stats")
async def kb_stats(user: CurrentUser = Depends(get_current_user)):
    """KB（context_snapshots の wasabi_*）統計"""
    try:
        docs = sc.list_context_snapshots()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore 読み取り失敗: {e}")

    wasabi = [(d, data) for d, data in docs if d.startswith("wasabi_")]
    by_type: dict[str, int] = {}
    embedded = 0
    latest = ""
    for _, data in wasabi:
        st = data.get("source_type", "?")
        by_type[st] = by_type.get(st, 0) + 1
        if "embedding" in data:
            embedded += 1
        synced = str(data.get("synced_at", ""))
        if synced > latest:
            latest = synced
    return {
        "total": len(wasabi),
        "embedded": embedded,
        "by_type": by_type,
        "latest_synced_at": latest[:19],
        "collection_total": len(docs),
    }
