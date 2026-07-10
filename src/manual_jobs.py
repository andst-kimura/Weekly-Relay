"""
手動実行ジョブ（Slack Bot から呼び出す）

- collect_meeting_docs : 議事録収集（Drive フォルダ + カレンダー添付）→ SmartSync KB 保存
- prepare_backlog_post : Backlog 転記のプレビュー用データ収集
- execute_backlog_post : Backlog 転記の実行

週次バッチ（main.run_weekly_report）と同じ部品を再利用する薄いラッパー。
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  議事録収集
# --------------------------------------------------------------------------- #

def collect_meeting_docs(config: dict, since: datetime, until: datetime) -> dict:
    """議事録を2ルートで収集し SmartSync KB に保存する。

    ルート①: Drive フォルダ（Meet Recordings、自分がオーナー）
    ルート②: Google カレンダー添付（自分がオーナーでない会議も含む）

    戻り値: {"count": int, "titles": [str], "errors": [str]}
    """
    from src.google_calendar_client import GoogleCalendarClient
    from src.google_docs_client import GoogleDocsClient
    from src.gemini_client import GeminiClient
    from src.knowledge_base import KnowledgeBase

    errors: list[str] = []
    cfg_cal = config.get("google_calendar", {})
    cfg_meet = config.get("google_meet", {})
    cfg_gemini = config.get("gemini", {})

    # カレンダーイベント取得（ルート②の入力）
    calendar_events = []
    try:
        cal_client = GoogleCalendarClient(
            credentials_file=cfg_cal["credentials_file"],
            calendar_ids=cfg_cal.get("calendar_ids", ["primary"]),
        )
        calendar_events = cal_client.get_events(since, until)
        logger.info(f"手動議事録収集: カレンダーイベント {len(calendar_events)} 件")
    except Exception as e:
        errors.append(f"カレンダー取得失敗: {e}")
        logger.warning(f"手動議事録収集: カレンダー取得失敗 {e}")

    docs_client = GoogleDocsClient(credentials_file=cfg_cal["credentials_file"])

    # ルート①: Drive フォルダ
    folder_docs = []
    if cfg_meet.get("folder_id"):
        try:
            folder_docs = docs_client.get_meeting_docs(
                folder_id=cfg_meet["folder_id"], since=since, until=until)
            logger.info(f"手動議事録収集: フォルダ {len(folder_docs)} 件")
        except Exception as e:
            errors.append(f"フォルダ取得失敗: {e}")
            logger.warning(f"手動議事録収集: フォルダ取得失敗 {e}")

    # ルート②: カレンダー添付
    calendar_docs = []
    try:
        calendar_docs = docs_client.get_docs_from_events(calendar_events)
        logger.info(f"手動議事録収集: カレンダー添付 {len(calendar_docs)} 件")
    except Exception as e:
        errors.append(f"カレンダー添付取得失敗: {e}")
        logger.warning(f"手動議事録収集: カレンダー添付取得失敗 {e}")

    meeting_docs = docs_client.merge_docs(folder_docs, calendar_docs)
    if not meeting_docs:
        return {"count": 0, "titles": [], "errors": errors}

    # SmartSync KB へ保存（KnowledgeBase の議事録処理を流用）
    gemini_client = GeminiClient(
        api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
        model=cfg_gemini.get("model", "gemini-2.5-flash"),
    )
    vector_store = None
    if gemini_client.enabled:
        from src.vector_store import VectorStore
        vector_store = VectorStore(embed_fn=gemini_client.embed)

    kb = KnowledgeBase(
        backlog_client=None, slack_client=None,
        gemini_client=gemini_client, vector_store=vector_store,
    )
    kb._generate_meeting_knowledge(meeting_docs)

    titles = [
        f"{d.get('title', '')}（{d['created_date'].strftime('%m/%d')}）"
        for d in meeting_docs
    ]
    return {"count": len(meeting_docs), "titles": titles, "errors": errors}


# --------------------------------------------------------------------------- #
#  ジョブ実行記録（鮮度チェック用）
# --------------------------------------------------------------------------- #

# この時間以内に完了済みなら再収集をスキップする（分）
FRESHNESS_MINUTES = 30

_JOB_STATUS_COLLECTION = "wasabi_job_status"


def get_last_success(job: str) -> datetime | None:
    """ジョブの最終成功時刻を返す（記録がなければ None）"""
    from src import smartsync_client as sc
    from datetime import timezone
    try:
        url = sc._doc_url(_JOB_STATUS_COLLECTION, job)
        resp = sc._get_session().get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = sc._parse_doc(resp.json()) or {}
        ts = data.get("completed_at")
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return ts if isinstance(ts, datetime) else None
    except Exception as e:
        logger.warning(f"job_status 取得失敗（{job}）: {e}")
        return None


def mark_success(job: str, detail: str = "") -> None:
    """ジョブの成功を記録する"""
    from src import smartsync_client as sc
    from datetime import timezone
    try:
        sc.save_doc(_JOB_STATUS_COLLECTION, job, {
            "completed_at": datetime.now(timezone.utc),
            "detail": detail,
        })
    except Exception as e:
        logger.warning(f"job_status 記録失敗（{job}）: {e}")


def is_fresh(job: str, minutes: int = FRESHNESS_MINUTES) -> tuple[bool, datetime | None]:
    """直近 minutes 分以内に成功済みかを返す"""
    from datetime import timezone, timedelta
    last = get_last_success(job)
    if not last:
        return False, None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    fresh = datetime.now(timezone.utc) - last < timedelta(minutes=minutes)
    return fresh, last


# --------------------------------------------------------------------------- #
#  全 KB 収集（Backlog + Slack + 議事録）
# --------------------------------------------------------------------------- #

def collect_kb(config: dict, since: datetime, until: datetime) -> dict:
    """Backlog・Slack・議事録をまとめて収集し SmartSync KB に保存する。

    週次バッチの KB 生成部分（main.run_weekly_report セクション8）と同等の処理。
    戻り値: {"backlog": int, "slack_channels": "N/A", "meeting": int,
             "members": int, "errors": [str]}
    """
    from main import _collect_all_data
    from src.backlog_client import BacklogClient
    from src.slack_client import SlackClient
    from src.gemini_client import GeminiClient
    from src.knowledge_base import KnowledgeBase
    from src.team_config import load_team

    errors: list[str] = []
    cfg_backlog = config["backlog"]
    cfg_slack = config["slack"]
    cfg_gemini = config.get("gemini", {})

    backlog, slack, events, meeting = _collect_all_data(config, since, until, use_cache=True)

    bc = BacklogClient(base_url=cfg_backlog["base_url"], api_key=cfg_backlog["api_key"],
                       my_user_id=cfg_backlog["my_user_id"])
    slack_client = SlackClient(bot_token=cfg_slack["bot_token"],
                               my_user_id=cfg_slack["my_user_id"])

    # チームメンバー分の Backlog 活動を追加収集（wasabi_teams 優先）
    team = load_team(config=config)
    team_members = team.get("members") or config.get("team_members", [])
    for member in team_members:
        bid = member.get("backlog_user_id")
        if not bid:
            continue
        try:
            member_acts = bc.get_all_activities_for_user(
                bid, since, until,
                cfg_backlog.get("target_projects"),
                team.get("exclude_projects") or cfg_backlog.get("exclude_projects"))
            backlog.extend(member_acts)
        except Exception as e:
            errors.append(f"メンバー活動収集失敗（{member.get('name')}）: {e}")

    gemini_client = GeminiClient(
        api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
        model=cfg_gemini.get("model", "gemini-2.5-flash"),
    )
    vector_store = None
    if gemini_client.enabled:
        from src.vector_store import VectorStore
        vector_store = VectorStore(embed_fn=gemini_client.embed)

    kb = KnowledgeBase(
        backlog_client=bc, slack_client=slack_client,
        gemini_client=gemini_client, vector_store=vector_store,
        team_members=team_members,
    )
    kb.generate(backlog, since, until, meeting_docs=meeting)

    # SmartSync 日次同期分など embedding 未設定のドキュメントを検索対象化
    backfilled = 0
    if vector_store:
        try:
            backfilled = backfill_embeddings(vector_store)
        except Exception as e:
            errors.append(f"embedding 補完失敗: {e}")

    return {
        "backlog": len(backlog),
        "meeting": len(meeting),
        "members": len(team_members),
        "backfilled": backfilled,
        "errors": errors,
    }


def backfill_embeddings(vector_store) -> int:
    """embedding 未設定の KB ドキュメント（SmartSync 収集分含む）をベクトル化する。

    SmartSync の日次同期で作られたドキュメントは embedding を持たないため、
    収集ジョブの最後に呼んで RAG 検索対象に含める。戻り値は処理件数。
    """
    from src.smartsync_client import list_context_snapshots_without_embedding
    docs = list_context_snapshots_without_embedding()
    if not docs:
        return 0
    logger.info(f"embedding 補完: {len(docs)} 件")
    for doc_id, data in docs:
        try:
            vector_store.upsert(doc_id, data)
        except Exception as e:
            logger.warning(f"embedding 補完失敗 ({doc_id}): {e}")
    return len(docs)


# --------------------------------------------------------------------------- #
#  Backlog 転記
# --------------------------------------------------------------------------- #

def prepare_backlog_post(config: dict, week_start: datetime, week_end: datetime) -> dict:
    """転記プレビュー用のデータ収集（キャッシュ有効）。

    戻り値: {
        "week_start", "week_end",
        "backlog_count", "slack_count", "meeting_count", "memo_count",
        "targets": [str],   # channel_mapping の明示転記先一覧
        "data": {"backlog": [...], "slack": [...], "events": [...], "meeting": [...]},
    }
    """
    from main import _collect_all_data
    from src.smartsync_store import SmartSyncStore

    backlog, slack, events, meeting = _collect_all_data(config, week_start, week_end, use_cache=True)
    data = {"backlog": backlog, "slack": slack, "events": events, "meeting": meeting}

    # 手動メモ件数
    memo_count = 0
    try:
        store = SmartSyncStore()
        memo_count = len(store.get_manual_memos(week_start, week_end))
    except Exception as e:
        logger.warning(f"手動メモ件数取得失敗: {e}")

    # channel_mapping の明示転記先（チームプロファイル参照）
    from src.team_config import load_team
    team = load_team(config=config)
    targets = []
    for ch_name, mapping in (team.get("channel_mapping") or {}).items():
        parent = mapping.get("parent_issue_key")
        if parent:
            targets.append(f"#{ch_name} → {parent}")
    if not team.get("transfer_enabled", True):
        targets.insert(0, "⚠️ チーム設定で転記機能が無効になっています")

    return {
        "week_start": week_start,
        "week_end": week_end,
        "backlog_count": len(data.get("backlog", [])),
        "slack_count": len(data.get("slack", [])),
        "meeting_count": len(data.get("meeting", [])),
        "memo_count": memo_count,
        "targets": targets,
        "data": data,
    }


def execute_backlog_post(config: dict, prepared: dict) -> list[dict]:
    """prepare_backlog_post の結果を使って Backlog へ転記する。"""
    from src.backlog_client import BacklogClient
    from src.backlog_poster import BacklogPoster
    from src.report_generator import ReportGenerator
    from src.gemini_client import GeminiClient
    from src.smartsync_store import SmartSyncStore

    cfg_backlog = config["backlog"]
    cfg_slack = config["slack"]
    cfg_report = config.get("report", {})
    cfg_gemini = config.get("gemini", {})

    data = prepared["data"]
    week_start = prepared["week_start"]
    week_end = prepared["week_end"]

    backlog_client = BacklogClient(
        base_url=cfg_backlog["base_url"],
        api_key=cfg_backlog["api_key"],
        my_user_id=cfg_backlog["my_user_id"],
    )
    gemini_client = GeminiClient(
        api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
        model=cfg_gemini.get("model", "gemini-2.5-flash"),
    )

    generator = ReportGenerator()
    aggregated = generator.aggregate(
        data["backlog"], data["slack"], data["events"], week_start, week_end)
    generator.pre_summarize_meetings(data["meeting"], gemini_client)
    comment_text = generator.build_backlog_comment(
        aggregated, meeting_docs=data["meeting"], gemini_client=gemini_client)

    store = None
    try:
        store = SmartSyncStore()
    except Exception as e:
        logger.warning(f"SmartSyncStore 初期化失敗: {e}")

    from src.team_config import load_team
    team = load_team(config=config)
    if not team.get("transfer_enabled", True):
        raise RuntimeError("チーム設定で転記機能が無効になっています（wasabi_teams）")
    mode = team.get("transfer_mode", "single_project")
    if mode != "single_project":
        raise RuntimeError(f"未対応の転記方式です: {mode}（single_project のみ対応）")

    poster = BacklogPoster(
        client=backlog_client,
        report_project_key=team.get("report_project_key") or cfg_backlog["report_project_key"],
        channel_mapping=team.get("channel_mapping") or {},
        dry_run=cfg_report.get("dry_run", False),
    )
    results = poster.post_weekly_report(
        comment_text, data["backlog"], data["slack"], week_start, week_end,
        aggregated=aggregated, generator=generator,
        meeting_docs=data["meeting"], gemini_client=gemini_client,
        firestore_client=store,
    )
    return results
