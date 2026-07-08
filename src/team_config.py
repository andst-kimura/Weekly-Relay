"""
チームプロファイル（wasabi_teams コレクション）の読み書き

Wasabi の多チーム展開用設定を SmartSync Firestore の wasabi_teams で管理する。
未登録の場合は config.yaml の現行設定を "sales" チームとして合成して返す
（フォールバック・既存動作を壊さない）。

スキーマ（wasabi_teams/{team_id}）:
  team_name          : str
  active             : bool
  transfer_enabled   : bool     … Backlog 転記機能の利用有無
  report_project_key : str      … 転記先 Backlog プロジェクト
  channel_mapping    : dict     … {channel_name: {channel_id, parent_issue_key, label,
                                    related_meeting_keywords[], project_key}}
  members            : list     … [{name, backlog_user_id, slack_user_id}]
  admin_slack_ids    : list[str]… Bot 設定コマンドを実行できる Slack ユーザー
  google_meet_folder_id : str
  exclude_projects   : list[str]
  shared_info        : dict     … {project_key, issue_type_id}
  updated_at / updated_by      … 監査
"""
import logging
from datetime import datetime, timezone

from src import smartsync_client as sc

logger = logging.getLogger(__name__)

DEFAULT_TEAM_ID = "sales"
_COLLECTION = "wasabi_teams"


def _from_config_yaml(config: dict) -> dict:
    """config.yaml の現行設定から sales チームのプロファイルを合成する（フォールバック）"""
    cfg_backlog = config.get("backlog", {})
    cfg_slack = config.get("slack", {})
    cfg_meet = config.get("google_meet", {})
    cfg_bot = config.get("slack_bot", {})
    return {
        "team_name": "販売チーム",
        "active": True,
        "transfer_enabled": config.get("report", {}).get("auto_post_to_backlog", True),
        # 転記方式（予約フィールド）:
        #   single_project    … 進捗管理プロジェクトの親課題へコメント転記（現行方式）
        #   per_issue_project … 案件ごとのプロジェクトへ個別起票（未実装・将来対応）
        "transfer_mode": "single_project",
        "report_project_key": cfg_backlog.get("report_project_key", "SALES_TEAM"),
        "channel_mapping": cfg_slack.get("channel_mapping", {}) or {},
        "members": config.get("team_members", []) or [],
        "admin_slack_ids": [cfg_slack.get("my_user_id", "")] if cfg_slack.get("my_user_id") else [],
        "google_meet_folder_id": cfg_meet.get("folder_id", ""),
        "exclude_projects": cfg_backlog.get("exclude_projects", []) or [],
        "shared_info": cfg_bot.get("shared_info", {}) or {},
    }


def load_team(team_id: str = DEFAULT_TEAM_ID, config: dict = None) -> dict:
    """チームプロファイルを取得する。

    wasabi_teams に存在すればそれを返す。
    存在しない（未移行）場合は config.yaml の設定から合成して返す。
    """
    try:
        url = sc._doc_url(_COLLECTION, team_id)
        resp = sc._get_session().get(url, timeout=30)
        if resp.status_code == 200:
            data = sc._parse_doc(resp.json())
            if data:
                data["team_id"] = team_id
                return data
        elif resp.status_code != 404:
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"wasabi_teams 読み取り失敗（config.yaml にフォールバック）: {e}")

    if config is None:
        logger.warning(f"wasabi_teams/{team_id} 未登録・config 未指定のため空プロファイルを返します")
        return {"team_id": team_id, "active": False}

    profile = _from_config_yaml(config)
    profile["team_id"] = team_id
    logger.info(f"wasabi_teams/{team_id} 未登録のため config.yaml から合成しました")
    return profile


def list_teams() -> list[dict]:
    """全チームプロファイルを返す"""
    try:
        results = []
        for doc_id, data in sc.list_docs(_COLLECTION):
            data["team_id"] = doc_id
            results.append(data)
        return results
    except Exception as e:
        logger.warning(f"wasabi_teams 一覧取得失敗: {e}")
        return []


def save_team(team_id: str, data: dict, updated_by: str = "") -> None:
    """チームプロファイルを upsert する（updated_at / updated_by を自動付与）"""
    payload = {k: v for k, v in data.items() if k != "team_id"}
    payload["updated_at"] = datetime.now(timezone.utc)
    payload["updated_by"] = updated_by
    sc.save_doc(_COLLECTION, team_id, payload)
    logger.info(f"wasabi_teams/{team_id} 保存（by {updated_by or 'system'}）")


def write_audit(actor: str, action: str, target: str, detail: dict = None) -> None:
    """監査ログを wasabi_audit_logs に追記する"""
    try:
        sc.add_doc("wasabi_audit_logs", {
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail or {},
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning(f"監査ログ書き込み失敗: {e}")
