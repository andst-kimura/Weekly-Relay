"""
Slack Bot - KB への自然言語質問に答えるインタラクティブ Bot
Socket Mode で動作するため、パブリック URL 不要。

起動: python main.py --bot
対応イベント:
  - app_mention: チャンネル内でメンションされた質問
  - message (im): DM で送られた質問

コマンド一覧:
  質問      : @Wasabi Bot ACE刷新の進捗は？
  進捗メモ  : @Wasabi Bot メモ [ISSUE-KEY]: 内容
  共有事項  : @Wasabi Bot 共有事項 [タイトル]: 内容
  削除      : 返信スレッドで @Wasabi Bot delete
"""
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

JST_TZ = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)

_DELETE_COMMANDS = {"delete", "削除", "del", "消して", "消去"}
_MEMO_PREFIXES = ("メモ", "memo", "進捗メモ", "手動メモ", "進捗入力")
_SHARED_INFO_PREFIXES = ("共有事項", "共有", "shared")
_COLLECT_PREFIXES = ("議事録収集", "議事録取得", "minutes")
_KB_COLLECT_PREFIXES = ("情報収集", "kb収集", "全収集", "collect")
_POST_PREFIXES = ("backlog転記", "転記", "post")
_SETTINGS_PREFIXES = ("設定確認", "設定", "config")
_MYID_PREFIXES = ("私のid", "自分のid", "myid", "my id")
_TIMELINE_PREFIXES = ("経緯", "timeline")
# 期間指定パターン（例: 7/1-7/7, 07/01〜07/07）
_RANGE_RE = re.compile(
    r"(?P<m1>\d{1,2})/(?P<d1>\d{1,2})\s*[-〜~]\s*(?P<m2>\d{1,2})/(?P<d2>\d{1,2})"
)
# 課題キーのパターン（例: SALES_TEAM-23, MOBILEPOS-45）
_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
# 指示語（直前の会話への参照）パターン。これを含む質問のみ履歴を検索クエリに結合する
_ANAPHORA_RE = re.compile(
    r"それ|その|そっち|あれ|あの件|この件|これら|上記|前述|先ほど|さっき|同件|続き|再度|もう一度|他には|ほかに")
# 日付パターン（期限抽出用）: YYYY/MM/DD, YYYY-MM-DD, MM/DD, M/D
_DATE_RE = re.compile(
    r"(?:(?P<y>\d{4})[/-])?(?P<m>\d{1,2})[/-](?P<d>\d{1,2})"
)

_HELP_TEXT = """\
🦜 *Wasabi Bot コマンド一覧*
チャンネルでは `@Wasabi Bot コマンド`、DM ではそのまま送信してください。

*🔍 調べる*
```質問文をそのまま送信      … KB を検索して AI が回答（出典リンク付き）
経緯 SALES_TEAM-27       … 課題の時系列サマリー（引き継ぎ・報告用）```
　◦ 絞り込み: `種別:議事録 期間:今週 ポスタスの議論`（種別: 議事録/チケット/slack、期間: 今週/先週/今月/N日/M/D-M/D）
　◦ スレッド内・DM の追い質問は直前の会話の文脈を引き継ぎます

*✏️ 記録する*
```メモ SALES_TEAM-23: 内容  … 進捗メモを登録（週次レポートに反映・課題キー省略可）
共有事項 タイトル: 内容    … Backlog に課題を起票（あなたが担当者に）```

*⚙️ 実行する*
```情報収集 [7/1-7/7]        … Backlog+Slack+議事録を KB 化（期間省略時: 直近7日）
議事録収集 [7/1-7/7]      … 議事録のみ KB 化
転記 [7/1-7/4]            … 週次レポートを Backlog へ転記（プレビュー→ボタン確認）```

*🔧 設定・その他*
```設定確認                  … チーム設定を表示（全員可）
設定 転記 オン/オフ        … 転記機能の切替（チーム管理者）
設定 マッピング SALES_TEAM-27 … このチャンネルの転記先を設定（チャンネル内で・管理者）
私のID                     … 自分の Slack ID を確認
delete                     … Bot の返信を削除（返信スレッド内で送信）```

💡 機能の詳細はこの Bot の *ホームタブ* にも掲載しています。
"""


# フィルタ構文: 種別:議事録 / 期間:今週 等
_TYPE_FILTER_RE = re.compile(r"種別[:：]\s*(議事録|会議|チケット|backlog|slack|meeting)", re.IGNORECASE)
_PERIOD_FILTER_RE = re.compile(
    r"期間[:：]\s*(今週|今月|先週|\d+日|\d{1,2}/\d{1,2}\s*[-〜~]\s*\d{1,2}/\d{1,2})")
_TYPE_MAP = {
    "議事録": "meeting", "会議": "meeting", "meeting": "meeting",
    "チケット": "backlog", "backlog": "backlog",
    "slack": "slack",
}


def _extract_filters(text: str) -> tuple[str, dict]:
    """質問文から `種別:` `期間:` フィルタを抽出し、(残りの質問文, filters) を返す"""
    filters: dict = {}
    now = datetime.now()

    m = _TYPE_FILTER_RE.search(text)
    if m:
        filters["source_type"] = _TYPE_MAP.get(m.group(1).lower(), "")
        text = _TYPE_FILTER_RE.sub("", text)

    m = _PERIOD_FILTER_RE.search(text)
    if m:
        token = m.group(1).replace(" ", "")
        if token == "今週":
            monday = now - timedelta(days=now.weekday())
            filters["since"] = monday.strftime("%Y-%m-%d")
        elif token == "先週":
            monday = now - timedelta(days=now.weekday())
            filters["since"] = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
            filters["until"] = (monday - timedelta(days=1)).strftime("%Y-%m-%d")
        elif token == "今月":
            filters["since"] = now.strftime("%Y-%m-01")
        elif token.endswith("日"):
            days = int(token[:-1])
            filters["since"] = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        else:
            since, until = _parse_range(token)
            filters["since"] = since.strftime("%Y-%m-%d")
            filters["until"] = until.strftime("%Y-%m-%d")
        text = _PERIOD_FILTER_RE.sub("", text)

    return text.strip(" 　"), filters


def _parse_range(text: str, default_days: int = 7) -> tuple[datetime, datetime]:
    """テキストから 'M/D-M/D' 形式の期間を抽出する。なければ直近 default_days 日。"""
    now = datetime.now()
    m = _RANGE_RE.search(text)
    if m:
        year = now.year
        since = datetime(year, int(m.group("m1")), int(m.group("d1")), 0, 0, 0)
        until = datetime(year, int(m.group("m2")), int(m.group("d2")), 23, 59, 59)
        # 年またぎ（12月指定を1月に実行等）は前年とみなす
        if since > now:
            since = since.replace(year=year - 1)
            until = until.replace(year=year - 1)
        return since, until
    return now - timedelta(days=default_days), now


def _build_home_blocks() -> list[dict]:
    """App Home タブに表示する機能一覧（Block Kit）"""
    def section(md):
        return {"type": "section", "text": {"type": "mrkdwn", "text": md}}

    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🦜 Wasabi Bot"}},
        section(
            "販売チームの業務 AI アシスタントです。\n"
            "Backlog・Slack・Google Meet 議事録を毎週自動収集してナレッジベース（KB）を構築し、"
            "質問応答・週次レポートの Backlog 転記を行います。"
        ),
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "💬 KB 質問（いつでも）"}},
        section(
            "チャンネルでメンション、または DM で質問すると KB を検索して AI が回答します。\n"
            "```@Wasabi Bot ACE刷新の進捗は？```"
        ),
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "📝 進捗メモ・共有事項"}},
        section(
            "*進捗メモ*（週次レポートの情報源に追加）\n"
            "```メモ SALES_TEAM-23: ポスタスのエラーは解消済み```\n"
            "*共有事項*（Backlog に課題を起票）\n"
            "```共有事項 日程変更について: 6/30→7/7に延期```"
        ),
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "🔧 手動実行コマンド"}},
        section(
            "*情報収集*（Backlog + Slack + 議事録をまとめて KB 保存）\n"
            "```情報収集           … 直近7日分\n情報収集 7/1-7/7   … 期間指定```\n"
            "*議事録収集*（議事録のみ収集 → KB 保存）\n"
            "```議事録収集          … 直近7日分\n議事録収集 7/1-7/7  … 期間指定```\n"
            "*Backlog 転記*（プレビュー確認 → ボタンで実行）\n"
            "```転記               … 今週分\n転記 7/1-7/4       … 期間指定```"
        ),
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "⚙️ 設定コマンド"}},
        section(
            "```設定確認                       … 現在のチーム設定を表示（全員可）\n"
            "設定 転記 オン/オフ             … 転記機能の切替（チーム管理者）\n"
            "設定 マッピング SALES_TEAM-27  … このチャンネルの転記先を設定（チーム管理者）\n"
            "設定 マッピング削除             … このチャンネルの転記先を解除（チーム管理者）\n"
            "私のID                          … 自分の Slack ID を確認```"
        ),
        {"type": "divider"},
        section(
            "*その他*\n"
            "・`ヘルプ` … コマンド一覧を表示\n"
            "・返信スレッドで `delete` … Bot の返信を削除\n"
            "・毎週金曜 18:00 に週次レポートが自動実行されます"
        ),
    ]


def _extract_due_date(text: str) -> str | None:
    """本文から最初の日付を抽出して 'YYYY-MM-DD' 形式で返す。見つからなければ None。"""
    import datetime
    today = datetime.date.today()
    m = _DATE_RE.search(text)
    if not m:
        return None
    y = int(m.group("y")) if m.group("y") else today.year
    mo = int(m.group("m"))
    d = int(m.group("d"))
    try:
        return datetime.date(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


class SlackBot:
    """Socket Mode で動作する KB 質問応答 Bot"""

    def __init__(self, bot_token: str, app_token: str, vector_store, gemini_client,
                 n_results: int = 5, firestore_client=None, backlog_client=None,
                 shared_info_cfg: dict = None, slack_user_to_backlog: dict = None,
                 config: dict = None):
        self._bot_token = bot_token
        self._app_token = app_token
        self.vs = vector_store
        self.gemini = gemini_client
        self.n_results = n_results
        self.fs = firestore_client
        self.bl = backlog_client
        # shared_info_cfg: {"project_key": "SALES_TEAM", "issue_type_id": 915353}
        self._shared_info_cfg = shared_info_cfg or {}
        # slack_user_id -> backlog_user_id のマッピング（myself + team_members から構築）
        self._slack_to_backlog: dict[str, int] = slack_user_to_backlog or {}
        self._bot_user_id: str = ""
        # Backlog project_id キャッシュ
        self._project_id_cache: dict[str, int] = {}
        # 手動実行ジョブ用（議事録収集・転記）
        # 状態（ロック・プレビュー・完了待ち・削除用ts）は src/bot_state.py 経由で
        # Firestore に永続化する（再起動・マルチインスタンス対応）
        self._config = config or {}

    def _get_backlog_project_id(self, project_key: str) -> int | None:
        """プロジェクトキーから project_id を取得（キャッシュ付き）"""
        if project_key in self._project_id_cache:
            return self._project_id_cache[project_key]
        if not self.bl:
            return None
        try:
            project = self.bl.get_project(project_key)
            pid = project["id"]
            self._project_id_cache[project_key] = pid
            return pid
        except Exception as e:
            logger.warning(f"Backlog プロジェクト取得失敗 ({project_key}): {e}")
            return None

    def _handle_shared_info(self, raw_text: str, slack_user_id: str, channel: str) -> str:
        """共有事項コマンドを解析して Backlog に課題を起票し、KB にも保存する"""
        if not self.bl:
            return "⚠️ Backlog クライアントが設定されていないため起票できません。"
        if not self._shared_info_cfg.get("issue_type_id"):
            return "⚠️ config.yaml の `slack_bot.shared_info.issue_type_id` が未設定です。"

        # プレフィックスを除去
        text = raw_text
        for prefix in _SHARED_INFO_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].lstrip(" 　:：")
                break

        if not text:
            return (
                "共有事項の内容を入力してください。\n"
                "例: `@Wasabi Bot 共有事項 タイトル: 内容`"
            )

        # タイトルと本文を分離（「タイトル: 本文」 or 「: 本文」 or 「本文のみ」）
        # 区切りは ": " "：" ": " いずれか
        sep_m = re.search(r"[:：]\s*", text)
        if sep_m and sep_m.start() > 0:
            summary = text[:sep_m.start()].strip()
            body = text[sep_m.end():].strip()
        else:
            # タイトル省略 → 本文冒頭30文字をタイトルに
            body = text.strip()
            summary = body[:30] + ("…" if len(body) > 30 else "")

        if not body:
            body = summary

        # 期限を本文から抽出
        due_date = _extract_due_date(body)

        # Slack user → Backlog user
        assignee_id = self._slack_to_backlog.get(slack_user_id)

        # プロジェクト情報
        project_key = self._shared_info_cfg.get("project_key", "SALES_TEAM")
        issue_type_id = self._shared_info_cfg["issue_type_id"]
        project_id = self._get_backlog_project_id(project_key)
        if not project_id:
            return f"⚠️ Backlog プロジェクト `{project_key}` の取得に失敗しました。"

        # Backlog 課題説明文（投稿者・チャンネル情報を付記）
        description = (
            f"{body}\n\n"
            f"---\n"
            f"*Slack Bot（Wasabi）経由で起票*\n"
            f"投稿者 Slack ID: {slack_user_id} / チャンネル: {channel}"
        )

        try:
            issue = self.bl.create_issue(
                project_id=project_id,
                summary=summary,
                description=description,
                issue_type_id=issue_type_id,
                assignee_id=assignee_id,
                due_date=due_date,
            )
        except Exception as e:
            logger.error(f"Backlog 課題起票失敗: {e}")
            return f"⚠️ Backlog への起票に失敗しました: {e}"

        issue_key = issue.get("issueKey", "")
        base_url = self.bl.base_url
        issue_url = f"{base_url}/view/{issue_key}" if issue_key else ""

        # Firestore KB に保存
        if self.fs:
            try:
                self.fs.save_shared_info(
                    issue_key=issue_key,
                    summary=summary,
                    body=body,
                    slack_user_id=slack_user_id,
                    channel=channel,
                    due_date=due_date,
                )
            except Exception as e:
                logger.warning(f"共有事項 KB 保存失敗: {e}")

        assignee_note = f"\n・担当者: Backlog ID {assignee_id}" if assignee_id else ""
        due_note = f"\n・期限: {due_date}" if due_date else ""
        return (
            f"✅ 共有事項を Backlog に起票しました\n"
            f"・課題: {issue_url or issue_key}\n"
            f"・件名: {summary}"
            f"{assignee_note}"
            f"{due_note}"
        )

    def _handle_memo(self, raw_text: str, user: str, channel: str) -> str:
        """メモコマンドを解析して Firestore に保存し、確認メッセージを返す"""
        text = raw_text
        for prefix in _MEMO_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].lstrip(" 　:：")
                break

        issue_key = ""
        m = _ISSUE_KEY_RE.search(text)
        if m:
            issue_key = m.group(1)
            text = _ISSUE_KEY_RE.sub("", text, count=1).lstrip(" 　:：").strip()

        if not text:
            return "メモの内容を入力してください。\n例: `@Wasabi Bot メモ SALES_TEAM-23: 内容`"

        if self.fs:
            self.fs.save_manual_memo(
                text=text,
                parent_issue_key=issue_key,
                created_by=user,
                channel=channel,
            )
            issue_label = issue_key if issue_key else "未指定（週次レポート実行時に関連課題へ自動追加）"
            return (
                f"✅ 進捗メモを保存しました\n"
                f"・課題: {issue_label}\n"
                f"・内容: {text}\n"
                f"次の週次レポート実行時に情報源として使用されます。"
            )
        else:
            logger.warning(f"手動メモ: Firestore 未設定のため保存スキップ（{text}）")
            return "⚠️ Firestore が設定されていないため保存できませんでした。"

    # ------------------------------------------------------------------ #
    #  手動実行: 議事録収集
    # ------------------------------------------------------------------ #

    def _handle_collect_minutes(self, raw_text: str, say_fn, kwargs: dict,
                                  channel: str = "") -> None:
        """議事録収集をバックグラウンドで実行し、完了時にスレッドへ返信する"""
        from src import bot_state
        if not self._config:
            say_fn(text="⚠️ Bot に config が渡されていないため実行できません。", **kwargs)
            return
        if not bot_state.acquire_job_lock("collect_minutes"):
            bot_state.add_job_waiter(channel, kwargs.get("thread_ts", ""))
            say_fn(text="⏳ 現在別の収集ジョブが実行中です。完了したらお知らせします。", **kwargs)
            return

        since, until = _parse_range(raw_text)
        say_fn(
            text=f"🔄 議事録を収集中...（{since.strftime('%m/%d')}〜{until.strftime('%m/%d')}）\n"
                 f"完了したらこのスレッドに結果を返信します。",
            **kwargs,
        )

        def _worker():
            try:
                from src.manual_jobs import collect_meeting_docs
                result = collect_meeting_docs(self._config, since, until)
                lines = [f"✅ 議事録 {result['count']} 件を KB に保存しました"]
                lines += [f"　・{t}" for t in result["titles"][:15]]
                if len(result["titles"]) > 15:
                    lines.append(f"　…ほか {len(result['titles']) - 15} 件")
                if result["errors"]:
                    lines.append("⚠️ 一部エラー:")
                    lines += [f"　・{e}" for e in result["errors"]]
                if result["count"] == 0 and not result["errors"]:
                    lines = [f"議事録は見つかりませんでした（{since.strftime('%m/%d')}〜{until.strftime('%m/%d')}）"]
                message = "\n".join(lines)
                say_fn(text=message, **kwargs)
                self._notify_waiters(message)
            except Exception as e:
                logger.error(f"議事録収集失敗: {e}")
                say_fn(text=f"⚠️ 議事録収集に失敗しました: {e}", **kwargs)
                self._notify_waiters(f"⚠️ 実行中だった議事録収集は失敗しました: {e}")
            finally:
                from src import bot_state as _bs
                _bs.release_job_lock()

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  手動実行: 全 KB 収集（Backlog + Slack + 議事録）
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  経緯サマリーコマンド（課題の時系列まとめ）
    # ------------------------------------------------------------------ #

    def _handle_timeline(self, raw_text: str, say_fn, kwargs: dict) -> None:
        """課題の経緯サマリーをバックグラウンド生成してスレッドに返信する"""
        m = _ISSUE_KEY_RE.search(raw_text)
        if not m:
            say_fn(
                text="課題キーを指定してください。\n例: `経緯 SALES_TEAM-27`",
                **kwargs,
            )
            return
        if not self.bl:
            say_fn(text="⚠️ Backlog クライアントが設定されていないため実行できません。", **kwargs)
            return
        issue_key = m.group(1)
        say_fn(text=f"🔄 {issue_key} の経緯をまとめています...", **kwargs)

        def _worker():
            try:
                # Backlog API から最新の課題 + 全コメント（KB より鮮度が高い）
                issue = self.bl.get_issue(issue_key)
                comments = self.bl.get_all_comments(issue["id"])
                comments_text = "\n".join(
                    f"[{(c.get('created') or '')[:10]}] "
                    f"{(c.get('createdUser') or {}).get('name', '不明')}: "
                    f"{(c.get('content') or '')[:500]}"
                    for c in comments if (c.get("content") or "").strip()
                )
                # KB の ai_text を補助文脈に
                kb_text = ""
                try:
                    from src import smartsync_client as sc
                    from src.team_config import list_teams
                    team_ids = [t["team_id"] for t in list_teams()] or ["sales"]
                    for tid in team_ids:
                        doc = sc.get_context_snapshot(f"wasabi_{tid}_backlog_{issue_key}")
                        if doc:
                            kb_text = doc.get("ai_text", "")
                            break
                except Exception:
                    pass

                answer = self.gemini.summarize_timeline(
                    issue_key=issue_key,
                    summary=issue.get("summary", ""),
                    status=(issue.get("status") or {}).get("name", ""),
                    comments_text=comments_text,
                    kb_text=kb_text,
                )
                if not answer:
                    say_fn(text="⚠️ 経緯サマリーの生成に失敗しました。", **kwargs)
                    return
                issue_url = f"{self.bl.base_url}/view/{issue_key}"
                say_fn(
                    text=f"📜 *{issue_key} {issue.get('summary', '')}*\n\n{answer}"
                         f"\n\n🔗 <{issue_url}|Backlog で開く>",
                    **kwargs,
                )
            except Exception as e:
                logger.error(f"経緯サマリー失敗 ({issue_key}): {e}")
                say_fn(text=f"⚠️ 経緯サマリーの生成に失敗しました: {e}", **kwargs)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  設定コマンド（wasabi_teams の簡易設定）
    # ------------------------------------------------------------------ #

    def _is_team_admin(self, team: dict, slack_user_id: str) -> bool:
        return slack_user_id in (team.get("admin_slack_ids") or [])

    def _channel_name(self, channel_id: str) -> str:
        """チャンネル ID から名前を取得する（DM の場合は空文字）"""
        if not channel_id or channel_id.startswith("D"):
            return ""
        cached = getattr(self, "_channel_name_cache", None)
        if cached is None:
            cached = self._channel_name_cache = {}
        if channel_id in cached:
            return cached[channel_id]
        try:
            from slack_sdk import WebClient
            info = WebClient(token=self._bot_token).conversations_info(channel=channel_id)
            name = info.get("channel", {}).get("name", "")
        except Exception as e:
            logger.warning(f"チャンネル名取得失敗 ({channel_id}): {e}")
            name = ""
        cached[channel_id] = name
        return name

    def _handle_settings(self, raw_text: str, slack_user_id: str,
                          channel: str, channel_name: str) -> str:
        """設定コマンドを処理する。

        設定確認                       … 閲覧（全員可）
        設定 転記 オン/オフ            … 転記機能の切替（チーム管理者のみ）
        設定 マッピング SALES_TEAM-27  … 実行チャンネルをマッピング（チーム管理者のみ）
        設定 マッピング削除            … 実行チャンネルのマッピング解除（チーム管理者のみ）
        """
        from src.team_config import load_team, save_team, write_audit

        team = load_team(config=self._config)
        team_id = team.get("team_id", "sales")

        # プレフィックス除去
        text = raw_text
        for prefix in ("設定確認", "設定", "config"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip(" 　")
                break

        # --- 設定確認（引数なし or「確認」）---
        if not text or text in ("確認", "show"):
            mapping = team.get("channel_mapping") or {}
            mapped = [f"　　#{ch} → {m.get('parent_issue_key') or '（自動判別）'}"
                      for ch, m in mapping.items() if m.get("parent_issue_key")]
            return (
                f"⚙️ *{team.get('team_name', team_id)} の設定*\n"
                f"・転記機能: {'✅ 有効' if team.get('transfer_enabled') else '❌ 無効'}\n"
                f"・転記先: {team.get('report_project_key') or '未設定'}\n"
                f"・チャンネルマッピング: {len(mapping)} 件（うち明示転記先 {len(mapped)} 件）\n"
                + ("\n".join(mapped) + "\n" if mapped else "")
                + f"・メンバー: {len(team.get('members') or [])} 名\n"
                f"詳細な編集は管理画面から行えます。"
            )

        # --- 以降は変更系（チーム管理者のみ）---
        if not self._is_team_admin(team, slack_user_id):
            return ("⚠️ 設定の変更にはチーム管理者権限が必要です。\n"
                    "管理者は管理画面の「Bot 設定コマンド許可」で追加できます。")

        # --- 転記 オン/オフ ---
        m = re.match(r"転記\s*(オン|オフ|on|off|有効|無効)$", text, re.IGNORECASE)
        if m:
            val = m.group(1).lower() in ("オン", "on", "有効")
            merged = {k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")}
            merged["transfer_enabled"] = val
            save_team(team_id, merged, updated_by=f"slack:{slack_user_id}")
            write_audit(f"slack:{slack_user_id}", "update_transfer_enabled", team_id, {"value": val})
            return (f"✅ 転記機能を{'有効' if val else '無効'}にしました。"
                    + ("" if val else "週次実行では KB 収集のみ行います。"))

        # --- マッピング削除（このチャンネル）---
        if text in ("マッピング削除", "mapping delete"):
            if not channel_name:
                return "⚠️ このコマンドは対象チャンネル内で実行してください（DM では使えません）。"
            mapping = dict(team.get("channel_mapping") or {})
            if channel_name not in mapping:
                return f"#{channel_name} のマッピングは登録されていません。"
            del mapping[channel_name]
            merged = {k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")}
            merged["channel_mapping"] = mapping
            save_team(team_id, merged, updated_by=f"slack:{slack_user_id}")
            write_audit(f"slack:{slack_user_id}", "delete_mapping", team_id, {"channel": channel_name})
            return f"✅ #{channel_name} のマッピングを削除しました。"

        # --- マッピング追加（このチャンネル → 課題キー）---
        m = re.match(r"マッピング\s+([A-Z][A-Z0-9_]+-\d+)$", text)
        if m:
            issue_key = m.group(1)
            if not channel_name:
                return "⚠️ このコマンドは対象チャンネル内で実行してください（DM では使えません）。"
            # Backlog で実在検証 + 課題名取得
            label = ""
            if self.bl:
                try:
                    issue = self.bl.get_issue(issue_key)
                    label = issue.get("summary", "")[:60]
                except Exception:
                    return f"⚠️ Backlog 課題 {issue_key} が見つかりません。"
            mapping = dict(team.get("channel_mapping") or {})
            mapping[channel_name] = {
                **(mapping.get(channel_name) or {}),
                "channel_id": channel,
                "parent_issue_key": issue_key,
                "label": label,
            }
            merged = {k: v for k, v in team.items() if k not in ("team_id", "updated_at", "updated_by")}
            merged["channel_mapping"] = mapping
            save_team(team_id, merged, updated_by=f"slack:{slack_user_id}")
            write_audit(f"slack:{slack_user_id}", "add_mapping", team_id,
                        {"channel": channel_name, "issue": issue_key})
            return (f"✅ マッピングを設定しました\n"
                    f"　#{channel_name} → {issue_key}{f'（{label}）' if label else ''}")

        return ("設定コマンドの使い方:\n"
                "・`設定確認` … 現在の設定を表示\n"
                "・`設定 転記 オン` / `設定 転記 オフ`\n"
                "・（チャンネル内で）`設定 マッピング SALES_TEAM-27`\n"
                "・（チャンネル内で）`設定 マッピング削除`")

    def _notify_waiters(self, message: str) -> None:
        """ジョブ完了を待っている全ユーザーへ通知する（Firestore の待機リストから）"""
        from src import bot_state
        try:
            waiters = bot_state.pop_job_waiters()
        except Exception as e:
            logger.warning(f"待機リスト取得失敗: {e}")
            return
        if not waiters:
            return
        try:
            from slack_sdk import WebClient
            client = WebClient(token=self._bot_token)
            for w in waiters:
                params = {"channel": w["channel"], "text": message}
                if w.get("thread_ts"):
                    params["thread_ts"] = w["thread_ts"]
                client.chat_postMessage(**params)
        except Exception as e:
            logger.warning(f"待機者への通知失敗: {e}")

    def _handle_collect_kb(self, raw_text: str, say_fn, kwargs: dict,
                             channel: str = "") -> None:
        """Backlog・Slack・議事録の全収集をバックグラウンドで実行する"""
        from src import bot_state
        if not self._config:
            say_fn(text="⚠️ Bot に config が渡されていないため実行できません。", **kwargs)
            return

        from src.manual_jobs import is_fresh, FRESHNESS_MINUTES
        has_range = bool(_RANGE_RE.search(raw_text))
        if not has_range:
            # 期間指定なしの場合のみ鮮度チェック（明示指定は意図があるとみなし実行）
            fresh, last = is_fresh("collect_kb")
            if fresh and last:
                jst = last.astimezone(JST_TZ) if last.tzinfo else last
                say_fn(
                    text=f"✅ {jst.strftime('%H:%M')} に収集済みのため KB は最新です"
                         f"（{FRESHNESS_MINUTES}分以内の再収集はスキップされます）。\n"
                         f"強制的に再収集する場合は期間を指定してください（例: `情報収集 7/1-7/8`）。",
                    **kwargs,
                )
                return

        if not bot_state.acquire_job_lock("collect_kb"):
            bot_state.add_job_waiter(channel, kwargs.get("thread_ts", ""))
            say_fn(text="⏳ 現在別の収集ジョブが実行中です。完了したらお知らせします。", **kwargs)
            return

        since, until = _parse_range(raw_text)
        say_fn(
            text=f"🔄 情報収集を開始しました（{since.strftime('%m/%d')}〜{until.strftime('%m/%d')}）\n"
                 f"Backlog・Slack・議事録を収集して KB に保存します。数分かかる場合があります。",
            **kwargs,
        )

        def _worker():
            try:
                from src.manual_jobs import collect_kb, mark_success
                result = collect_kb(self._config, since, until)
                lines = [
                    "✅ 情報収集が完了しました",
                    f"・Backlog 活動: {result['backlog']} 件（メンバー {result['members']} 名分含む）",
                    f"・議事録: {result['meeting']} 件",
                    "・Slack: 参加チャンネルを収集済み",
                    "収集した情報は KB に保存され、質問応答で利用できます。",
                ]
                if result["errors"]:
                    lines.append("⚠️ 一部エラー:")
                    lines += [f"　・{e}" for e in result["errors"]]
                message = "\n".join(lines)
                mark_success("collect_kb", detail=f"backlog={result['backlog']}, meeting={result['meeting']}")
                say_fn(text=message, **kwargs)
                self._notify_waiters(message)
            except Exception as e:
                logger.error(f"情報収集失敗: {e}")
                say_fn(text=f"⚠️ 情報収集に失敗しました: {e}", **kwargs)
                self._notify_waiters(f"⚠️ 実行中だった情報収集は失敗しました: {e}")
            finally:
                from src import bot_state as _bs
                _bs.release_job_lock()

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  手動実行: Backlog 転記（プレビュー → ボタン確認）
    # ------------------------------------------------------------------ #

    def _handle_post_preview(self, raw_text: str, say_fn, kwargs: dict,
                               channel: str = "") -> None:
        """転記プレビューを生成し、確認ボタン付きで返信する"""
        from src import bot_state
        if not self._config:
            say_fn(text="⚠️ Bot に config が渡されていないため実行できません。", **kwargs)
            return
        if not bot_state.acquire_job_lock("post_preview"):
            say_fn(text="⏳ 別のジョブが実行中です。完了までお待ちください。", **kwargs)
            return

        # 期間: 指定なしなら今週（月曜0:00〜今日）
        m = _RANGE_RE.search(raw_text)
        if m:
            since, until = _parse_range(raw_text)
        else:
            now = datetime.now()
            monday = now - timedelta(days=now.weekday())
            since = monday.replace(hour=0, minute=0, second=0, microsecond=0)
            until = now

        say_fn(
            text=f"🔄 転記プレビューを作成中...（{since.strftime('%m/%d')}〜{until.strftime('%m/%d')}）",
            **kwargs,
        )

        def _worker():
            try:
                from src.manual_jobs import prepare_backlog_post
                prepared = prepare_backlog_post(self._config, since, until)
                pending_id = uuid.uuid4().hex[:12]
                # 期間メタのみ永続化（実行時に再収集する。キャッシュが効くため高速）
                bot_state.save_pending_post(pending_id, {
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                })

                dry_run = self._config.get("report", {}).get("dry_run", False)
                dry_note = "（dry_run 有効: 実際には書き込みません）" if dry_run else ""
                targets_text = "\n".join(f"　・{t}" for t in prepared["targets"][:10]) or "　（明示指定なし・自動判別）"
                preview_text = (
                    f"📋 *転記プレビュー*（{since.strftime('%m/%d')}〜{until.strftime('%m/%d')}）{dry_note}\n"
                    f"・Backlog 活動: {prepared['backlog_count']} 件\n"
                    f"・Slack メッセージ: {prepared['slack_count']} 件\n"
                    f"・議事録: {prepared['meeting_count']} 件\n"
                    f"・手動メモ: {prepared['memo_count']} 件\n"
                    f"*明示転記先（channel_mapping）*\n{targets_text}"
                )
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": preview_text}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "転記を実行"},
                                "style": "primary",
                                "action_id": "wasabi_post_confirm",
                                "value": pending_id,
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "キャンセル"},
                                "action_id": "wasabi_post_cancel",
                                "value": pending_id,
                            },
                        ],
                    },
                ]
                say_fn(text="転記プレビュー", blocks=blocks, **kwargs)
            except Exception as e:
                logger.error(f"転記プレビュー作成失敗: {e}")
                say_fn(text=f"⚠️ プレビュー作成に失敗しました: {e}", **kwargs)
            finally:
                bot_state.release_job_lock()

        threading.Thread(target=_worker, daemon=True).start()

    def _execute_post(self, pending_id: str, say_fn) -> None:
        """確認ボタン押下後の転記実行（バックグラウンド）"""
        from src import bot_state
        meta = bot_state.pop_pending_post(pending_id)
        if not meta:
            say_fn(text="⚠️ このプレビューは期限切れです。もう一度 `転記` コマンドを実行してください。")
            return
        if not bot_state.acquire_job_lock("execute_post"):
            say_fn(text="⏳ 別のジョブが実行中です。完了までお待ちください。")
            return

        say_fn(text="🔄 Backlog へ転記中...")

        def _worker():
            try:
                from src.manual_jobs import prepare_backlog_post, execute_backlog_post
                # プレビュー時の期間で再収集（当日キャッシュが効くため高速）
                since = datetime.fromisoformat(meta["since"])
                until = datetime.fromisoformat(meta["until"])
                prepared = prepare_backlog_post(self._config, since, until)
                results = execute_backlog_post(self._config, prepared)
                base_url = (self._config.get("backlog", {}) or {}).get(
                    "base_url", "https://adastria.backlog.jp").rstrip("/")
                _ACTION_LABEL = {"commented": "コメント転記", "created": "新規起票", "skipped": "スキップ"}
                lines = [f"✅ Backlog 転記が完了しました（{len(results)} 件）"]
                for r in results[:15]:
                    if isinstance(r, dict):
                        key = r.get("issue_key", "")
                        action = _ACTION_LABEL.get(r.get("action", ""), r.get("action", ""))
                        link = f"<{base_url}/view/{key}|{key}>" if key else "(不明)"
                        lines.append(f"　・{link}　{action}")
                    else:
                        lines.append(f"　・{str(r)[:80]}")
                if len(results) > 15:
                    lines.append(f"　…ほか {len(results) - 15} 件")
                lines.append("内容は各課題のコメントを確認してください。")
                say_fn(text="\n".join(lines))
            except Exception as e:
                logger.error(f"Backlog 転記失敗: {e}")
                say_fn(text=f"⚠️ 転記に失敗しました: {e}")
            finally:
                bot_state.release_job_lock()

        threading.Thread(target=_worker, daemon=True).start()

    def _format_sources(self, results: list[dict]) -> str:
        """検索結果から出典ブロック（リンク付き）を組み立てる"""
        base_url = (self._config.get("backlog", {}) or {}).get(
            "base_url", "https://adastria.backlog.jp").rstrip("/")
        lines = []
        seen: set[str] = set()
        for r in results:
            doc_id = r.get("doc_id", "")
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            meta = r.get("meta", {}) or {}
            st = meta.get("source_type", "")
            key = meta.get("source_key", "")
            name = meta.get("source_name", "")
            if st == "backlog" and key:
                if _ISSUE_KEY_RE.match(key):
                    # 課題キー → 課題への直接リンク
                    lines.append(f"・<{base_url}/view/{key}|{key}>　{name}")
                else:
                    # プロジェクトキー（SmartSync の集約ドキュメント）→ プロジェクトリンク
                    lines.append(f"・<{base_url}/projects/{key}|{name or key}>（プロジェクト週次まとめ）")
            elif st == "slack" and key:
                # Slack はチャンネルメンション形式（クリックでチャンネルへ）
                lines.append(f"・<#{key}> の週次まとめ")
            elif st == "meeting":
                url = meta.get("source_url", "")
                if url:
                    lines.append(f"・議事録: <{url}|{name or 'Google Docs'}>")
                else:
                    lines.append(f"・議事録: {name}")
            else:
                lines.append(f"・{name or doc_id}")
        if not lines:
            return ""
        return "\n\n📚 *情報源*\n" + "\n".join(lines)

    def _fetch_thread_history(self, channel: str, thread_ts: str,
                                current_ts: str = "", limit: int = 8) -> str:
        """スレッド内の直前のやり取りをテキスト化して返す（文脈継続用）"""
        if not thread_ts:
            return ""
        try:
            from slack_sdk import WebClient
            resp = WebClient(token=self._bot_token).conversations_replies(
                channel=channel, ts=thread_ts, limit=limit + 2)
            lines = []
            for msg in resp.get("messages", []):
                if msg.get("ts") == current_ts:
                    continue  # いま処理中の質問自体は除外
                text = re.sub(r"<@[^>]+>", "", msg.get("text") or "").strip()
                if not text:
                    continue
                speaker = "Bot" if (msg.get("bot_id") or msg.get("user") == self._bot_user_id) else "ユーザー"
                # 出典ブロックは履歴に不要なので除去
                text = text.split("📚")[0].strip()
                # 情報を含まない返答は文脈として無意味なので除外
                if "該当情報がありません" in text or text.startswith(("🔄", "⏳", "✅", "⚠️")):
                    continue
                lines.append(f"{speaker}: {text[:300]}")
            return "\n".join(lines[-limit:])
        except Exception as e:
            logger.warning(f"スレッド履歴取得失敗: {e}")
            return ""

    def _fetch_dm_history(self, channel: str, current_ts: str = "", limit: int = 6) -> str:
        """DM のフラットな会話の直近のやり取りをテキスト化して返す（文脈継続用）。

        DM ではスレッドを使わず連続して質問されることが多いため、
        直近メッセージを履歴として扱う。コマンド実行や古いメッセージは除外。
        """
        try:
            from slack_sdk import WebClient
            resp = WebClient(token=self._bot_token).conversations_history(
                channel=channel, limit=limit + 2)
            import time as _time
            lines = []
            for msg in reversed(resp.get("messages", [])):  # 古い順に
                if msg.get("ts") == current_ts:
                    continue
                # 30分より古いやり取りは文脈にしない（別トピックの可能性が高い）
                if current_ts and float(current_ts) - float(msg.get("ts", 0)) > 1800:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                speaker = "Bot" if (msg.get("bot_id") or msg.get("user") == self._bot_user_id) else "ユーザー"
                text = text.split("📚")[0].strip()
                if "該当情報がありません" in text or text.startswith(("🔄", "⏳", "✅", "⚠️")):
                    continue
                lines.append(f"{speaker}: {text[:300]}")
            return "\n".join(lines[-limit:])
        except Exception as e:
            logger.warning(f"DM 履歴取得失敗: {e}")
            return ""

    def _search_and_answer(self, query: str, history: str = "") -> str:
        """クエリを KB 検索 → Gemini で回答生成（出典リンク付き・スレッド文脈対応）"""
        if self.vs.count() == 0:
            return (
                "KB にデータがありません。\n"
                "`python main.py --only kb` を実行してインデックスを構築してください。"
            )
        # フィルタ構文（種別: / 期間:）を抽出
        query, filters = _extract_filters(query)
        # 指示語（それ・その 等）を含む質問のみ、履歴と結合して検索クエリを補強する。
        # 単に短いだけの独立した質問に履歴を混ぜると、無関係な話題に検索が引きずられるため
        is_anaphoric = bool(_ANAPHORA_RE.search(query))
        search_query = f"{history}\n{query}" if (history and is_anaphoric) else query
        results = self.vs.search(search_query, n_results=self.n_results, filters=filters)
        if not results:
            return "KB に該当情報が見つかりませんでした。"
        answer = self.gemini.answer_with_context(query, results, history=history)
        if not answer:
            return "回答の生成に失敗しました。"
        # 「該当情報なし」の場合は出典を付けない
        if "該当情報がありません" in answer:
            return answer
        return answer + self._format_sources(results)

    def _dispatch(self, text: str, user: str, channel: str, say_fn,
                   thread_ts: str = None, history: str = ""):
        """コマンド判定と処理の共通ロジック。thread_ts=None は DM。
        history: スレッド内の直前のやり取り（RAG の文脈継続に使用）"""
        kwargs = {"thread_ts": thread_ts} if thread_ts else {}

        # 削除コマンド
        if text.lower() in _DELETE_COMMANDS:
            from src import bot_state
            key = thread_ts if thread_ts else channel
            bot_ts = bot_state.pop_reply_ts(key)
            if bot_ts:
                # say_fn の client は外側スコープから渡せないため、削除は呼び出し元で処理
                return "_delete_", bot_ts
            say_fn(
                text="削除対象の Bot 返信が見つかりませんでした。\n"
                     "Bot 再起動後は返信の記録がリセットされます。",
                **kwargs,
            )
            return None, None

        # ヘルプ
        if not text or text in ("help", "ヘルプ", "?", "？"):
            say_fn(text=_HELP_TEXT, **kwargs)
            return None, None

        # 議事録収集コマンド
        if any(text.lower().startswith(p.lower()) for p in _COLLECT_PREFIXES):
            self._handle_collect_minutes(text, say_fn, kwargs, channel=channel)
            return None, None

        # 全 KB 収集コマンド（Backlog + Slack + 議事録）
        if any(text.lower().startswith(p.lower()) for p in _KB_COLLECT_PREFIXES):
            self._handle_collect_kb(text, say_fn, kwargs, channel=channel)
            return None, None

        # Backlog 転記コマンド（プレビュー → ボタン確認）
        if any(text.lower().startswith(p.lower()) for p in _POST_PREFIXES):
            self._handle_post_preview(text, say_fn, kwargs, channel=channel)
            return None, None

        # 経緯サマリーコマンド
        if any(text.lower().startswith(p.lower()) for p in _TIMELINE_PREFIXES):
            self._handle_timeline(text, say_fn, kwargs)
            return None, None

        # 私のID（メンバー登録用の Slack ID 確認）
        if text.lower() in _MYID_PREFIXES:
            ch_name = self._channel_name(channel)
            ch_note = f" / このチャンネル: {channel}（#{ch_name}）" if ch_name else ""
            say_fn(text=f"🦜 あなたの Slack ID: `{user}`{ch_note}", **kwargs)
            return None, None

        # 設定コマンド（wasabi_teams の簡易設定）
        if any(text.lower().startswith(p.lower()) for p in _SETTINGS_PREFIXES):
            reply = self._handle_settings(text, user, channel, self._channel_name(channel))
            say_fn(text=reply, **kwargs)
            return None, None

        # 共有事項コマンド
        if any(text.lower().startswith(p.lower()) for p in _SHARED_INFO_PREFIXES):
            reply = self._handle_shared_info(text, user, channel)
            say_fn(text=reply, **kwargs)
            return None, None

        # メモコマンド
        if any(text.lower().startswith(p.lower()) for p in _MEMO_PREFIXES):
            reply = self._handle_memo(text, user, channel)
            say_fn(text=reply, **kwargs)
            return None, None

        # KB 質問（RAG）
        logger.info(f"Slack Bot 質問受信: {text}")
        answer = self._search_and_answer(text, history=history)
        resp = say_fn(text=answer, **kwargs)
        if resp and resp.get("ts"):
            from src import bot_state
            store_key = thread_ts if thread_ts else channel
            bot_state.save_reply_ts(store_key, resp["ts"])
        return None, None

    def run(self):
        """Socket Mode で Bot を起動（ブロッキング）"""
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError:
            logger.error(
                "slack-bolt が未インストールです。`pip install slack-bolt` を実行してください。"
            )
            return

        app = App(token=self._bot_token)

        try:
            self._bot_user_id = app.client.auth_test()["user_id"]
            logger.info(f"Slack Bot user_id: {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Bot user_id 取得失敗: {e}")

        @app.event("app_mention")
        def handle_mention(event, client, say):
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
            thread_ts = event.get("thread_ts") or event.get("ts")
            user = event.get("user", "")
            channel = event["channel"]

            # スレッド内のメンションなら直前のやり取りを文脈として取得
            history = ""
            if event.get("thread_ts"):
                history = self._fetch_thread_history(
                    channel, event["thread_ts"], current_ts=event.get("ts", ""))

            cmd, bot_ts = self._dispatch(text, user, channel, say,
                                          thread_ts=event.get("ts"), history=history)
            if cmd == "_delete_":
                # thread_ts がある場合はスレッドの根 ts がキー
                key = event.get("thread_ts")
                if key:
                    from src import bot_state
                    actual_ts = bot_state.pop_reply_ts(key) or bot_ts
                else:
                    actual_ts = bot_ts
                try:
                    client.chat_delete(channel=channel, ts=actual_ts)
                    logger.info(f"Bot メッセージ削除: ts={actual_ts}")
                except Exception as e:
                    logger.warning(f"chat_delete 失敗: {e}")
                    say(text=f"削除に失敗しました: {e}", thread_ts=event.get("ts"))

        @app.event("message")
        def handle_dm(event, client, say):
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()
            if not text:
                return

            user = event.get("user", "")
            channel = event["channel"]

            # DM の文脈取得: スレッド返信ならスレッド履歴、フラットな連続質問なら直近の会話
            if event.get("thread_ts"):
                history = self._fetch_thread_history(
                    channel, event["thread_ts"], current_ts=event.get("ts", ""))
            else:
                history = self._fetch_dm_history(channel, current_ts=event.get("ts", ""))

            cmd, bot_ts = self._dispatch(text, user, channel, say,
                                          thread_ts=None, history=history)
            if cmd == "_delete_":
                try:
                    client.chat_delete(channel=channel, ts=bot_ts)
                    logger.info(f"DM Bot メッセージ削除: ts={bot_ts}")
                except Exception as e:
                    logger.warning(f"chat_delete 失敗: {e}")
                    say(f"削除に失敗しました: {e}")

        @app.action("wasabi_post_confirm")
        def handle_post_confirm(ack, body, client, say):
            ack()
            pending_id = body["actions"][0]["value"]
            channel = body["channel"]["id"]
            msg = body.get("message", {}) or {}
            thread_ts = msg.get("thread_ts")

            # ボタンを消して二度押しを防ぐ
            try:
                client.chat_update(
                    channel=channel, ts=msg.get("ts"),
                    text="転記を実行します...", blocks=[])
            except Exception:
                pass

            def _say(text, **kw):
                params = {"channel": channel, "text": text}
                if thread_ts:
                    params["thread_ts"] = thread_ts
                return client.chat_postMessage(**params)

            self._execute_post(pending_id, _say)

        @app.action("wasabi_post_cancel")
        def handle_post_cancel(ack, body, client):
            ack()
            pending_id = body["actions"][0]["value"]
            from src import bot_state
            bot_state.pop_pending_post(pending_id)
            channel = body["channel"]["id"]
            msg = body.get("message", {}) or {}
            try:
                client.chat_update(
                    channel=channel, ts=msg.get("ts"),
                    text="❌ 転記をキャンセルしました。", blocks=[])
            except Exception:
                client.chat_postMessage(channel=channel, text="❌ 転記をキャンセルしました。")

        @app.event("app_home_opened")
        def handle_app_home(event, client):
            try:
                client.views_publish(
                    user_id=event["user"],
                    view={"type": "home", "blocks": _build_home_blocks()},
                )
            except Exception as e:
                logger.warning(f"App Home 表示失敗: {e}")

        logger.info("Slack Bot 起動中... (Socket Mode)")
        logger.info("チャンネル内でメンション、またはDMで質問できます。Ctrl+C で停止。")
        SocketModeHandler(app, self._app_token).start()
