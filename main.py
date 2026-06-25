"""
週次進捗報告 自動化ツール - メインスクリプト
毎週金曜18時に自動実行、または手動実行可能
"""
import truststore
truststore.inject_into_ssl()  # Windows証明書ストアを使用（社内プロキシ対応）

import yaml
import logging
import logging.handlers
import argparse
import schedule
import time
import os
import re
import jpholiday
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

from src.backlog_client import BacklogClient
from src.slack_client import SlackClient
from src.google_calendar_client import GoogleCalendarClient
from src.report_generator import ReportGenerator
from src.backlog_poster import BacklogPoster
from src.knowledge_base import KnowledgeBase
from src.ticket_alert import TicketAlert
from src.daily_summary import DailySummary
from src.google_docs_client import GoogleDocsClient
from src.gemini_client import GeminiClient
from src.cleanup import CleanupTool
from src.firestore_client import FirestoreClient

# ログ設定
Path("output").mkdir(exist_ok=True)
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_handler = logging.handlers.RotatingFileHandler(
    "output/run.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])
logger = logging.getLogger(__name__)

# Google SDK / httpx / gRPC / absl の冗長ログを抑制
# "AFC is enabled" は google 親ロガー配下から出るため google 全体を WARNING に設定
for _noisy in ("httpx", "httpcore", "google", "grpc", "grpc._channel", "absl",
               "googleapiclient.discovery_cache"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# absl-py は独自ロギングを持つため個別に抑制
try:
    import absl.logging as _absl_log
    _absl_log.set_verbosity(_absl_log.WARNING)
except ImportError:
    pass


def is_business_day(dt: datetime = None) -> bool:
    """今日が営業日かどうか判定（土日・祝日を除く）"""
    d = (dt or datetime.now()).date()
    return d.weekday() < 5 and not jpholiday.is_holiday(d)


def load_config(path: str = "config/config.yaml") -> dict:
    load_dotenv()
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # ${VAR_NAME} を環境変数で置換
    content = re.sub(
        r'\$\{(\w+)\}',
        lambda m: os.environ.get(m.group(1), m.group(0)),
        content,
    )
    return yaml.safe_load(content)


def get_week_range(reference_dt: datetime = None) -> tuple[datetime, datetime]:
    """今週月曜00:00〜金曜18:00の範囲を返す"""
    now = reference_dt or datetime.now()
    # 今週の月曜日
    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = now.replace(hour=18, minute=0, second=0, microsecond=0)

    # 月曜日の場合は先週分を対象にする
    if now.weekday() == 0:
        week_start = week_start - timedelta(days=7)
        week_end = week_end - timedelta(days=3)  # 先週金曜18:00

    return week_start, week_end


def run_weekly_report(config: dict, reference_dt: datetime = None):
    """メイン処理: データ収集 → レポート生成 → Backlog転記"""
    import time as _time
    _started_at = _time.monotonic()

    logger.info("=" * 60)
    logger.info("Weekly Relay 開始")
    logger.info("=" * 60)

    week_start, week_end = get_week_range(reference_dt)
    logger.info(f"対象期間: {week_start} 〜 {week_end}")

    cfg_backlog = config["backlog"]
    cfg_slack = config["slack"]
    cfg_cal = config["google_calendar"]
    cfg_report = config["report"]

    # ------------------------------------------------------------------ #
    # 1. Backlog データ収集
    # ------------------------------------------------------------------ #
    logger.info("\n--- Backlog データ収集 ---")
    backlog_client = BacklogClient(
        base_url=cfg_backlog["base_url"],
        api_key=cfg_backlog["api_key"],
        my_user_id=cfg_backlog["my_user_id"],
    )
    target_projects = cfg_backlog.get("target_projects", None)
    exclude_projects = cfg_backlog.get("exclude_projects", None)
    backlog_activities = backlog_client.get_all_my_activities(week_start, week_end, target_projects, exclude_projects)
    logger.info(f"Backlog: {len(backlog_activities)} 件の活動を取得")

    # ------------------------------------------------------------------ #
    # 2. Slack データ収集
    # ------------------------------------------------------------------ #
    logger.info("\n--- Slack データ収集 ---")
    slack_client = SlackClient(
        bot_token=cfg_slack["bot_token"],
        my_user_id=cfg_slack["my_user_id"],
    )
    slack_messages = slack_client.get_all_my_messages(week_start, week_end)
    logger.info(f"Slack: {len(slack_messages)} 件のメッセージを取得")

    # ------------------------------------------------------------------ #
    # 3. Google Calendar データ収集
    # ------------------------------------------------------------------ #
    logger.info("\n--- Google Calendar データ収集 ---")
    try:
        cal_client = GoogleCalendarClient(
            credentials_file=cfg_cal["credentials_file"],
            calendar_ids=cfg_cal["calendar_ids"],
        )
        calendar_events = cal_client.get_events(week_start, week_end)
        logger.info(f"Calendar: {len(calendar_events)} 件のイベントを取得")
    except Exception as e:
        logger.warning(f"Google Calendar の取得に失敗（スキップ）: {e}")
        calendar_events = []

    # ------------------------------------------------------------------ #
    # 4. Google Meet 議事録収集
    # ------------------------------------------------------------------ #
    cfg_meet = config.get("google_meet", {})
    meeting_docs = []
    if cfg_meet.get("enabled", False):
        logger.info("\n--- Google Meet 議事録収集 ---")
        try:
            docs_client = GoogleDocsClient(
                credentials_file=cfg_cal["credentials_file"],
            )

            # ① 自分がオーナーの議事録：Meet Recordings フォルダから取得
            folder_docs = []
            if cfg_meet.get("folder_id"):
                folder_docs = docs_client.get_meeting_docs(
                    folder_id=cfg_meet["folder_id"],
                    since=week_start,
                    until=week_end,
                )
                logger.info(f"  フォルダ（自分がオーナー）: {len(folder_docs)} 件")

            # ② 参加した全 MTG の議事録：カレンダー添付ファイルから取得
            calendar_docs = docs_client.get_docs_from_events(calendar_events)
            logger.info(f"  カレンダー添付（参加した全MTG）: {len(calendar_docs)} 件")

            # 重複排除してマージ（IDが同じドキュメントは1件に統合）
            meeting_docs = docs_client.merge_docs(folder_docs, calendar_docs)
            logger.info(f"議事録: 合計 {len(meeting_docs)} 件（重複排除済み）")

        except Exception as e:
            logger.warning(f"Google Meet 議事録の取得に失敗（スキップ）: {e}")

    # ------------------------------------------------------------------ #
    # 5. Gemini クライアント初期化
    # ------------------------------------------------------------------ #
    cfg_gemini = config.get("gemini", {})
    gemini_client = GeminiClient(
        api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
        model=cfg_gemini.get("model", "gemini-2.0-flash"),
    )

    # ------------------------------------------------------------------ #
    # 5b. Firestore クライアント初期化
    # ------------------------------------------------------------------ #
    firestore_client = None
    cfg_fs = config.get("firestore", {})
    if cfg_fs.get("enabled", False):
        try:
            firestore_client = FirestoreClient()
            logger.info("Firestore クライアント初期化完了")
        except Exception as e:
            logger.warning(f"Firestore 初期化失敗（ローカル出力にフォールバック）: {e}")

    # ------------------------------------------------------------------ #
    # 6. レポート生成
    # ------------------------------------------------------------------ #
    logger.info("\n--- レポート生成 ---")
    generator = ReportGenerator()

    aggregated = generator.aggregate(
        backlog_activities, slack_messages, calendar_events, week_start, week_end
    )

    comment_text = generator.build_backlog_comment(
        aggregated, meeting_docs=meeting_docs, gemini_client=gemini_client
    )

    # Firestore へ保存
    if firestore_client:
        firestore_client.save_weekly_report(week_start, week_end, comment_text)
        if calendar_events:
            firestore_client.save_calendar_report(calendar_events[0]["start_dt"], "\n".join(
                f"{ev['start_dt'].strftime('%Y/%m/%d %H:%M')} {ev['summary']} ({ev['duration_hours']}h)"
                for ev in calendar_events
            ))
    else:
        # Firestore 無効時はローカルファイルに保存
        local_path = generator.save_local_report(aggregated, comment_text, cfg_report["output_dir"])
        logger.info(f"ローカルレポート: {local_path}")
        cal_path = generator.save_calendar_report(calendar_events, cfg_report["output_dir"])
        if cal_path:
            logger.info(f"カレンダーレポート: {cal_path}")

    # ------------------------------------------------------------------ #
    # 7. Backlog 転記
    # ------------------------------------------------------------------ #
    if cfg_report.get("auto_post_to_backlog", True):
        logger.info("\n--- Backlog 転記 ---")
        poster = BacklogPoster(
            client=backlog_client,
            report_project_key=cfg_backlog["report_project_key"],
            channel_mapping=cfg_slack.get("channel_mapping", {}),
            dry_run=cfg_report.get("dry_run", False),
        )
        results = poster.post_weekly_report(
            comment_text, backlog_activities, slack_messages, week_start, week_end,
            aggregated=aggregated, generator=generator,
            meeting_docs=meeting_docs, gemini_client=gemini_client,
        )
        for r in results:
            logger.info(f"転記結果: {r}")

    # ------------------------------------------------------------------ #
    # 8. ナレッジベース生成
    # ------------------------------------------------------------------ #
    cfg_kb = config.get("knowledge_base", {})
    if cfg_kb.get("enabled", False):
        logger.info("\n--- ナレッジベース生成 ---")
        kb = KnowledgeBase(
            backlog_client=backlog_client,
            slack_client=slack_client,
            gemini_client=gemini_client,
            firestore_client=firestore_client,
        )
        kb.generate(backlog_activities, week_start, week_end, meeting_docs=meeting_docs)

    duration = _time.monotonic() - _started_at
    logger.info(f"\n✅ Weekly Relay 完了（所要時間: {duration:.1f}秒）")
    logger.info("=" * 60)

    if firestore_client:
        firestore_client.write_sync_log(
            status="success", job="weekly_report",
            detail=f"backlog={len(backlog_activities)}, slack={len(slack_messages)}, meetings={len(meeting_docs)}",
            duration_sec=round(duration, 1),
        )


def _make_clients(config: dict) -> tuple[BacklogClient, SlackClient]:
    """BacklogClient と SlackClient を生成するヘルパー"""
    cfg_b = config["backlog"]
    cfg_s = config["slack"]
    return (
        BacklogClient(base_url=cfg_b["base_url"], api_key=cfg_b["api_key"],
                      my_user_id=cfg_b["my_user_id"]),
        SlackClient(bot_token=cfg_s["bot_token"], my_user_id=cfg_s["my_user_id"]),
    )


def run_ticket_alert(config: dict) -> None:
    """未対応チケット警告（毎朝スケジューラーから呼ぶ）"""
    if not is_business_day():
        logger.info("本日は休日のため未対応チケット警告をスキップします")
        return
    logger.info("=" * 60)
    logger.info("未対応チケット警告 開始")
    backlog_client, slack_client = _make_clients(config)
    cfg_alert = config.get("ticket_alert", {})
    alert = TicketAlert(
        backlog_client=backlog_client,
        slack_client=slack_client,
        exclude_projects=config["backlog"].get("exclude_projects", []),
        stale_business_days=cfg_alert.get("stale_business_days", 3),
    )
    alert.run()
    logger.info("未対応チケット警告 完了")
    logger.info("=" * 60)


def run_daily_summary(config: dict) -> None:
    """日次夕方サマリー（毎夕スケジューラーから呼ぶ）"""
    if not is_business_day():
        logger.info("本日は休日のため日次サマリーをスキップします")
        return
    logger.info("=" * 60)
    logger.info("日次夕方サマリー 開始")
    backlog_client, slack_client = _make_clients(config)
    cfg_gemini = config.get("gemini", {})
    generator = ReportGenerator()
    gemini_client = GeminiClient(
        api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
        model=cfg_gemini.get("model", "gemini-2.5-flash"),
    )
    summary = DailySummary(
        backlog_client=backlog_client,
        slack_client=slack_client,
        generator=generator,
        exclude_projects=config["backlog"].get("exclude_projects", []),
        gemini_client=gemini_client,
    )
    summary.run()
    logger.info("日次夕方サマリー 完了")
    logger.info("=" * 60)


_ONLY_CHOICES = ["backlog", "slack", "calendar", "firestore", "report", "kb"]


def run_only_mode(config: dict, only: str, reference_dt: datetime = None) -> None:
    """--only モード: 特定機能のみ実行して動作確認する"""
    import time as _time

    week_start, week_end = get_week_range(reference_dt)
    cfg_backlog = config["backlog"]
    cfg_slack = config["slack"]
    cfg_cal = config["google_calendar"]
    cfg_meet = config.get("google_meet", {})
    cfg_gemini = config.get("gemini", {})
    cfg_fs = config.get("firestore", {})

    logger.info("=" * 60)
    logger.info(f"[DRY-RUN: --only {only}]  対象期間: {week_start} 〜 {week_end}")
    logger.info("=" * 60)

    # ---------- Backlog ----------
    if only == "backlog":
        t = _time.monotonic()
        bc = BacklogClient(base_url=cfg_backlog["base_url"],
                           api_key=cfg_backlog["api_key"],
                           my_user_id=cfg_backlog["my_user_id"])
        acts = bc.get_all_my_activities(
            week_start, week_end,
            cfg_backlog.get("target_projects"),
            cfg_backlog.get("exclude_projects"),
        )
        logger.info(f"✅ Backlog: {len(acts)} 件取得 ({_time.monotonic()-t:.1f}s)")
        for a in acts[:10]:
            logger.info(f"  [{a['type']}] {a['issue_key']} {a['summary'][:50]}")
        if len(acts) > 10:
            logger.info(f"  ... 他 {len(acts)-10} 件")
        return

    # ---------- Slack ----------
    if only == "slack":
        t = _time.monotonic()
        sc = SlackClient(bot_token=cfg_slack["bot_token"],
                         my_user_id=cfg_slack["my_user_id"])
        msgs = sc.get_all_my_messages(week_start, week_end)
        logger.info(f"✅ Slack: {len(msgs)} 件取得 ({_time.monotonic()-t:.1f}s)")
        return

    # ---------- Calendar / 議事録 ----------
    if only == "calendar":
        t = _time.monotonic()
        cal = GoogleCalendarClient(credentials_file=cfg_cal["credentials_file"],
                                    calendar_ids=cfg_cal["calendar_ids"])
        events = cal.get_events(week_start, week_end)
        logger.info(f"✅ Calendar: {len(events)} 件取得 ({_time.monotonic()-t:.1f}s)")
        for ev in events:
            logger.info(f"  {ev['start_dt'].strftime('%m/%d %H:%M')} {ev['summary']}")

        if cfg_meet.get("enabled", False):
            docs_client = GoogleDocsClient(credentials_file=cfg_cal["credentials_file"])
            folder_docs = []
            if cfg_meet.get("folder_id"):
                folder_docs = docs_client.get_meeting_docs(
                    folder_id=cfg_meet["folder_id"], since=week_start, until=week_end)
            calendar_docs = docs_client.get_docs_from_events(events)
            meeting_docs = docs_client.merge_docs(folder_docs, calendar_docs)
            logger.info(f"✅ 議事録: {len(meeting_docs)} 件（重複排除済み）")
            for d in meeting_docs:
                logger.info(f"  {d['created_date'].strftime('%m/%d')} {d['title'][:50]}")
        return

    # ---------- Firestore ----------
    if only == "firestore":
        if not cfg_fs.get("enabled", False):
            logger.warning("config の firestore.enabled が false です")
            return
        t = _time.monotonic()
        try:
            fs = FirestoreClient()
            test_id = "dry-run-test"
            fs.save_context_snapshot(test_id, {"ping": "pong", "source_type": "test"})
            result = fs.get_context_snapshot(test_id)
            if result and result.get("ping") == "pong":
                logger.info(f"✅ Firestore: 書き込み・読み込み成功 ({_time.monotonic()-t:.1f}s)")
            else:
                logger.warning(f"⚠️ Firestore: 書き込みは成功したが読み込み結果が不正: {result}")
        except Exception as e:
            logger.error(f"❌ Firestore: 接続失敗 — {e}")
        return

    # ---------- Report（データ収集 + レポート生成のみ、Backlog転記なし）----------
    if only == "report":
        bc = BacklogClient(base_url=cfg_backlog["base_url"],
                           api_key=cfg_backlog["api_key"],
                           my_user_id=cfg_backlog["my_user_id"])
        sc = SlackClient(bot_token=cfg_slack["bot_token"],
                         my_user_id=cfg_slack["my_user_id"])
        acts = bc.get_all_my_activities(week_start, week_end,
                                         cfg_backlog.get("target_projects"),
                                         cfg_backlog.get("exclude_projects"))
        msgs = sc.get_all_my_messages(week_start, week_end)
        cal = GoogleCalendarClient(credentials_file=cfg_cal["credentials_file"],
                                    calendar_ids=cfg_cal["calendar_ids"])
        events = cal.get_events(week_start, week_end)
        meeting_docs = []
        if cfg_meet.get("enabled", False):
            docs_client = GoogleDocsClient(credentials_file=cfg_cal["credentials_file"])
            folder_docs = []
            if cfg_meet.get("folder_id"):
                folder_docs = docs_client.get_meeting_docs(
                    folder_id=cfg_meet["folder_id"], since=week_start, until=week_end)
            calendar_docs = docs_client.get_docs_from_events(events)
            meeting_docs = docs_client.merge_docs(folder_docs, calendar_docs)

        gemini = GeminiClient(
            api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
            model=cfg_gemini.get("model", "gemini-2.0-flash"),
        )
        generator = ReportGenerator()
        aggregated = generator.aggregate(acts, msgs, events, week_start, week_end)
        comment = generator.build_backlog_comment(
            aggregated, meeting_docs=meeting_docs, gemini_client=gemini)
        logger.info(f"✅ レポート生成完了（{len(comment)} 文字、Backlog転記はスキップ）")
        return

    # ---------- KB（ナレッジベース生成のみ）----------
    if only == "kb":
        cfg_kb = config.get("knowledge_base", {})
        if not cfg_kb.get("enabled", False):
            logger.warning("config の knowledge_base.enabled が false です")
            return
        bc = BacklogClient(base_url=cfg_backlog["base_url"],
                           api_key=cfg_backlog["api_key"],
                           my_user_id=cfg_backlog["my_user_id"])
        sc = SlackClient(bot_token=cfg_slack["bot_token"],
                         my_user_id=cfg_slack["my_user_id"])
        acts = bc.get_all_my_activities(week_start, week_end,
                                         cfg_backlog.get("target_projects"),
                                         cfg_backlog.get("exclude_projects"))
        meeting_docs = []
        if cfg_meet.get("enabled", False):
            cal = GoogleCalendarClient(credentials_file=cfg_cal["credentials_file"],
                                        calendar_ids=cfg_cal["calendar_ids"])
            events = cal.get_events(week_start, week_end)
            docs_client = GoogleDocsClient(credentials_file=cfg_cal["credentials_file"])
            folder_docs = []
            if cfg_meet.get("folder_id"):
                folder_docs = docs_client.get_meeting_docs(
                    folder_id=cfg_meet["folder_id"], since=week_start, until=week_end)
            calendar_docs = docs_client.get_docs_from_events(events)
            meeting_docs = docs_client.merge_docs(folder_docs, calendar_docs)

        gemini = GeminiClient(
            api_key=cfg_gemini.get("api_key", "") if cfg_gemini.get("enabled", False) else "",
            model=cfg_gemini.get("model", "gemini-2.0-flash"),
        )
        fs = None
        if cfg_fs.get("enabled", False):
            try:
                fs = FirestoreClient()
            except Exception as e:
                logger.warning(f"Firestore 初期化失敗: {e}")

        kb = KnowledgeBase(backlog_client=bc, slack_client=sc,
                           gemini_client=gemini, firestore_client=fs)
        kb.generate(acts, week_start, week_end, meeting_docs=meeting_docs)
        logger.info("✅ ナレッジベース生成完了")
        return


def main():
    parser = argparse.ArgumentParser(description="週次進捗報告 自動化ツール")
    parser.add_argument(
        "--run-now", action="store_true",
        help="スケジューラーを待たずに今すぐ実行"
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="設定ファイルのパス（デフォルト: config/config.yaml）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Backlogへの書き込みをスキップして動作確認"
    )
    parser.add_argument(
        "--only", choices=_ONLY_CHOICES, metavar="|".join(_ONLY_CHOICES),
        help="特定機能のみ実行: backlog / slack / calendar / firestore / report / kb"
    )
    parser.add_argument(
        "--check-user-id", action="store_true",
        help="BacklogとSlackのユーザーIDを確認して終了"
    )
    parser.add_argument(
        "--run-alert", action="store_true",
        help="未対応チケット警告を今すぐ実行"
    )
    parser.add_argument(
        "--run-summary", action="store_true",
        help="日次夕方サマリーを今すぐ実行"
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help="Weekly Relay が転記したコメント・課題を対話形式で削除する"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.dry_run:
        config["report"]["dry_run"] = True
        logger.info("🔍 ドライランモード: Backlogへの書き込みはスキップされます")

    if args.only:
        run_only_mode(config, args.only)
        return

    # ユーザーID確認モード
    if args.check_user_id:
        client = BacklogClient(
            base_url=config["backlog"]["base_url"],
            api_key=config["backlog"]["api_key"],
            my_user_id=0,
        )
        user_id = client.get_my_user_id()
        logger.info(f"✅ あなたのBacklogユーザーID: {user_id}")
        logger.info("config/config.yaml の backlog.my_user_id に上記の値を設定してください")
        return

    if args.run_alert:
        run_ticket_alert(config)
        return

    if args.run_summary:
        run_daily_summary(config)
        return

    if args.cleanup:
        cfg_backlog = config["backlog"]
        backlog_client = BacklogClient(
            base_url=cfg_backlog["base_url"],
            api_key=cfg_backlog["api_key"],
            my_user_id=cfg_backlog["my_user_id"],
        )
        CleanupTool(
            client=backlog_client,
            report_project_key=cfg_backlog["report_project_key"],
        ).run()
        return

    # 即時実行
    if args.run_now:
        run_weekly_report(config)
        return

    # スケジューラーで毎週金曜18時に実行
    cfg_sched = config.get("schedule", {})
    day = cfg_sched.get("day_of_week", "friday")
    hour = cfg_sched.get("hour", 18)
    minute = cfg_sched.get("minute", 0)
    run_time = f"{hour:02d}:{minute:02d}"

    logger.info(f"スケジューラー起動: 毎週{day} {run_time} に実行します")

    # 未対応チケット警告（毎朝）
    cfg_alert = config.get("ticket_alert", {})
    if cfg_alert.get("enabled", False):
        alert_time = f"{cfg_alert.get('run_hour', 9):02d}:{cfg_alert.get('run_minute', 0):02d}"
        schedule.every().day.at(alert_time).do(run_ticket_alert, config=config)
        logger.info(f"未対応チケット警告: 毎日 {alert_time}（土日祝スキップ）")

    # 日次夕方サマリー（毎夕）
    cfg_ds = config.get("daily_summary", {})
    if cfg_ds.get("enabled", False):
        summary_time = f"{cfg_ds.get('run_hour', 17):02d}:{cfg_ds.get('run_minute', 30):02d}"
        schedule.every().day.at(summary_time).do(run_daily_summary, config=config)
        logger.info(f"日次夕方サマリー: 毎日 {summary_time}（土日祝スキップ）")

    # 週次レポート（毎週金曜）
    getattr(schedule.every(), day).at(run_time).do(run_weekly_report, config=config)

    logger.info("Ctrl+C で停止")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
