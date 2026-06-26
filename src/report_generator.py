"""
レポート生成モジュール
Gemini API が使えない場合はルールベースの要約にフォールバック
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict
import logging

_SUMMARY_WORKERS = 10  # 議事録要約の並列数

logger = logging.getLogger(__name__)


class ReportGenerator:
    def __init__(self, claude_api_key: str = "", claude_enabled: bool = False,
                 claude_model: str = "claude-sonnet-4-6"):
        # Claude API パラメータは後方互換のため残すが使用しない（Gemini に移行済み）
        pass

    # ------------------------------------------------------------------ #
    #  データ集約
    # ------------------------------------------------------------------ #

    def aggregate(self, backlog_activities: list[dict], slack_messages: list[dict],
                  calendar_events: list[dict], week_start: datetime, week_end: datetime) -> dict:
        """全データを集約してレポート構造を作成"""

        # Backlogをプロジェクト > 課題でグループ化
        backlog_by_project = defaultdict(lambda: defaultdict(list))
        for act in backlog_activities:
            proj = act["project_name"]
            key = f"{act['issue_key']} {act['summary']}"
            backlog_by_project[proj][key].append(act)

        # Slackをチャンネルでグループ化
        slack_by_channel = defaultdict(list)
        for msg in slack_messages:
            slack_by_channel[msg["channel_name"]].append(msg)

        # カレンダーを日別集計
        total_calendar_hours = sum(e["duration_hours"] for e in calendar_events)

        return {
            "week_start": week_start,
            "week_end": week_end,
            "backlog_by_project": dict(backlog_by_project),
            "slack_by_channel": dict(slack_by_channel),
            "calendar_events": calendar_events,
            "total_calendar_hours": total_calendar_hours,
        }

    # ------------------------------------------------------------------ #
    #  Backlog転記用テキスト生成
    # ------------------------------------------------------------------ #

    def pre_summarize_meetings(self, meeting_docs: list[dict], gemini_client) -> None:
        """議事録要約を並列実行し、各 doc の _summary キーに格納する。"""
        if not meeting_docs or not gemini_client or not gemini_client.enabled:
            return
        targets = [d for d in meeting_docs if d.get("text") and "_summary" not in d]
        if not targets:
            return
        logger.info(f"議事録要約 並列開始: {len(targets)} 件 (workers={_SUMMARY_WORKERS})")

        def _summarize(doc):
            return doc, gemini_client.summarize_meeting(doc["text"])

        with ThreadPoolExecutor(max_workers=_SUMMARY_WORKERS) as executor:
            futures = {executor.submit(_summarize, d): d for d in targets}
            for future in as_completed(futures):
                try:
                    doc, summary = future.result()
                    doc["_summary"] = summary or ""
                except Exception as e:
                    doc = futures[future]
                    logger.warning(f"議事録要約失敗 ({doc.get('title', '?')}): {e}")
                    doc["_summary"] = ""
        logger.info("議事録要約 並列完了")

    def build_backlog_comment(self, aggregated: dict,
                               meeting_docs: list[dict] = None,
                               gemini_client=None) -> str:
        """Backlogコメント用のMarkdownテキストを生成"""
        w_start = aggregated["week_start"].strftime("%Y/%m/%d")
        w_end = aggregated["week_end"].strftime("%Y/%m/%d")

        lines = [
            f"## Weekly Relay 週次進捗レポート {w_start}〜{w_end}",
            "",
            "---",
            "",
        ]

        # Backlog活動
        if aggregated["backlog_by_project"]:
            lines.append("### 📋 Backlog 対応状況")
            lines.append("")
            for proj, issues in aggregated["backlog_by_project"].items():
                lines.append(f"**【{proj}】**")
                for issue_label, activities in issues.items():
                    statuses = list({a["status"] for a in activities if a.get("status")})
                    status_str = f"（{', '.join(statuses)}）" if statuses else ""
                    lines.append(f"- **{issue_label}** {status_str}")

                    # チケットの説明を追加
                    description = activities[0].get("description", "")
                    if description:
                        desc_clean = description.replace("\n", " ").replace("\r", "")
                        desc_clean = " ".join(desc_clean.split())
                        if len(desc_clean) > 100:
                            desc_clean = desc_clean[:100] + "…"
                        lines.append(f"  - 📋 概要: {desc_clean}")

                    # コメントをまとめて表示
                    comments = [a for a in activities if a["type"] == "comment" and a.get("comment_content")]
                    if comments:
                        lines.append(f"  - 💬 コメント（{len(comments)}件）:")
                        for act in comments:
                            content = act["comment_content"]
                            content = content.replace("\n", " ").replace("\r", "")
                            content = " ".join(content.split())
                            content = content.replace("{quote}", "「").replace("{/quote}", "」")
                            # 長いコメントは150文字で折り返し
                            if len(content) > 150:
                                content = content[:150] + "…"
                            updated = act.get("updated", "")
                            time_str = ""
                            if updated:
                                try:
                                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                                    time_str = f"`{dt.strftime('%m/%d %H:%M')}` "
                                except Exception:
                                    pass
                            lines.append(f"    - {time_str}{content}")

                    created = [a for a in activities if a["type"] == "created_issue"]
                    if created:
                        lines.append(f"  - ✅ 課題を新規作成")

                    lines.append("")

        # Slack活動
        if aggregated["slack_by_channel"]:
            lines.append("### 💬 Slack コミュニケーション")
            lines.append("")
            for channel, msgs in aggregated["slack_by_channel"].items():
                lines.append(f"**#{channel}**（{len(msgs)}件の発言）")
                for msg in sorted(msgs, key=lambda x: x["datetime"]):
                    text = msg["text"]
                    text = text.replace("\n", " ").replace("\r", "")
                    text = " ".join(text.split())
                    time_str = msg["datetime"].strftime("%m/%d %H:%M")
                    lines.append(f"  - `{time_str}` {text}")
                lines.append("")

        # カレンダー工数
        if aggregated["calendar_events"]:
            lines.append("### 📅 工数サマリー（Googleカレンダーより）")
            lines.append("")
            lines.append(f"**週間合計工数: {aggregated['total_calendar_hours']:.1f}時間**")
            lines.append("")

            # 日別
            by_day: dict = defaultdict(list)
            for ev in aggregated["calendar_events"]:
                day = ev["start_dt"].strftime("%m/%d(%a)")
                by_day[day].append(ev)

            for day, evs in sorted(by_day.items()):
                day_hours = sum(e["duration_hours"] for e in evs)
                lines.append(f"**{day}** ({day_hours:.1f}h)")
                for ev in evs:
                    lines.append(f"  - {ev['summary']}（{ev['duration_hours']}h）")
            lines.append("")

        # 議事録セクション
        if meeting_docs:
            lines.append("### 📝 会議・決定事項（Google Meet）")
            lines.append("")
            for doc in meeting_docs:
                date_str = doc["created_date"].strftime("%Y/%m/%d")
                lines.append(f"**{doc['title']}**（{date_str}）")
                # _summary は pre_summarize_meetings() で並列計算済み
                summary = doc.get("_summary")
                if summary is None and gemini_client and gemini_client.enabled and doc.get("text"):
                    summary = gemini_client.summarize_meeting(doc["text"])
                if summary:
                    lines.append(summary)
                else:
                    excerpt = doc.get("text", "")[:200].replace("\n", " ")
                    if excerpt:
                        lines.append(excerpt + ("…" if len(doc.get("text", "")) > 200 else ""))
                lines.append("")

        lines.append("---")
        lines.append("*このコメントは Weekly Relay により自動転記されました*")

        return "\n".join(lines)

    def build_comment_for_issue(self, issue_key: str, project_keys: list[str],
                                 channel_names: list[str], aggregated: dict,
                                 meeting_docs: list[dict] = None,
                                 gemini_client=None) -> str:
        """特定の親課題向けに関連データのみを抽出してコメントを生成"""
        w_start = aggregated["week_start"].strftime("%Y/%m/%d")
        w_end = aggregated["week_end"].strftime("%Y/%m/%d")

        lines = [
            f"## Weekly Relay 週次進捗レポート {w_start}〜{w_end}",
            "",
            "---",
            "",
        ]

        # 関連するBacklog活動のみ抽出
        related_backlog = {}
        for proj, issues in aggregated["backlog_by_project"].items():
            if proj in project_keys or any(
                issue_key_str.startswith(tuple(pk + "-" for pk in project_keys))
                for issue_key_str in issues.keys()
            ):
                related_backlog[proj] = issues

        if related_backlog:
            lines.append("### 📋 Backlog 対応状況")
            lines.append("")
            for proj, issues in related_backlog.items():
                lines.append(f"**【{proj}】**")
                for issue_label, activities in issues.items():
                    statuses = list({a["status"] for a in activities if a.get("status")})
                    status_str = f"（{', '.join(statuses)}）" if statuses else ""
                    lines.append(f"- **{issue_label}** {status_str}")

                    description = activities[0].get("description", "")
                    if description:
                        desc_clean = description.replace("\n", " ").replace("\r", "")
                        desc_clean = " ".join(desc_clean.split())
                        if len(desc_clean) > 100:
                            desc_clean = desc_clean[:100] + "…"
                        lines.append(f"  - 📋 概要: {desc_clean}")

                    comments = [a for a in activities if a["type"] == "comment" and a.get("comment_content")]
                    if comments:
                        lines.append(f"  - 💬 コメント（{len(comments)}件）:")
                        for act in comments:
                            content = act["comment_content"]
                            content = content.replace("\n", " ").replace("\r", "")
                            content = " ".join(content.split())
                            content = content.replace("{quote}", "「").replace("{/quote}", "」")
                            if len(content) > 150:
                                content = content[:150] + "…"
                            updated = act.get("updated", "")
                            time_str = ""
                            if updated:
                                try:
                                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                                    time_str = f"`{dt.strftime('%m/%d %H:%M')}` "
                                except Exception:
                                    pass
                            lines.append(f"    - {time_str}{content}")

                    if any(a["type"] == "created_issue" for a in activities):
                        lines.append(f"  - ✅ 課題を新規作成")
                    lines.append("")

        # 関連するSlackメッセージのみ抽出
        related_slack = {
            ch: msgs for ch, msgs in aggregated["slack_by_channel"].items()
            if ch in channel_names
        }

        if related_slack:
            lines.append("### 💬 Slack コミュニケーション")
            lines.append("")
            for channel, msgs in related_slack.items():
                lines.append(f"**#{channel}**（{len(msgs)}件の発言）")
                for msg in sorted(msgs, key=lambda x: x["datetime"]):
                    text = msg["text"].replace("\n", " ").replace("\r", "")
                    text = " ".join(text.split())
                    time_str = msg["datetime"].strftime("%m/%d %H:%M")
                    lines.append(f"  - `{time_str}` {text}")
                lines.append("")

        # 議事録セクション
        if meeting_docs:
            lines.append("### 📝 会議・決定事項（Google Meet）")
            lines.append("")
            for doc in meeting_docs:
                date_str = doc["created_date"].strftime("%Y/%m/%d")
                lines.append(f"**{doc['title']}**（{date_str}）")
                # _summary は pre_summarize_meetings() で並列計算済み
                summary = doc.get("_summary")
                if summary is None and gemini_client and gemini_client.enabled and doc.get("text"):
                    summary = gemini_client.summarize_meeting(doc["text"])
                if summary:
                    lines.append(summary)
                else:
                    excerpt = doc.get("text", "")[:200].replace("\n", " ")
                    if excerpt:
                        lines.append(excerpt + ("…" if len(doc.get("text", "")) > 200 else ""))
                lines.append("")

        lines.append("---")
        lines.append("*このコメントは Weekly Relay により自動転記されました*")
        return "\n".join(lines)

    def build_status_next_action(
        self,
        backlog_acts: list[dict],
        slack_msgs: list[dict],
        meeting_docs: list[dict],
        week_start: datetime,
        week_end: datetime,
        issue_summary: str = "",
        gemini_client=None,
        issue_keywords: list[str] = None,
        manual_memos: list[dict] = None,
    ) -> str:
        """
        =Status= / =NextAction= フォーマットのBacklogコメントを生成する。
        Gemini が有効な場合は AI 整形、無効な場合はルールベースにフォールバック。
        issue_keywords: 全体MTGの要約から関連行を絞り込むキーワード
        """
        w_start = week_start.strftime("%Y/%m/%d")
        w_end = week_end.strftime("%Y/%m/%d")
        header = f"## Weekly Relay 週次進捗レポート {w_start}〜{w_end}\n\n---\n\n"
        footer = "\n\n---\n*このコメントは Weekly Relay により自動転記されました*"

        if gemini_client and gemini_client.enabled:
            sources_text = self._build_sources_text(
                backlog_acts, slack_msgs, meeting_docs, w_start, w_end,
                issue_keywords=issue_keywords,
                manual_memos=manual_memos,
            )
            formatted = gemini_client.format_backlog_comment(sources_text, issue_summary)
            if formatted:
                return header + formatted + footer

        # ルールベースフォールバック
        return header + self._rule_based_status(
            backlog_acts, slack_msgs, meeting_docs
        ) + footer

    def _build_sources_text(
        self,
        backlog_acts: list[dict],
        slack_msgs: list[dict],
        meeting_docs: list[dict],
        w_start: str,
        w_end: str,
        issue_keywords: list[str] = None,
        manual_memos: list[dict] = None,
    ) -> str:
        """Gemini に渡すデータソーステキストを構築する。
        issue_keywords が指定された場合、議事録の要約を行単位でフィルタし
        キーワード関連行のみを渡す（全体MTGの無関係議題混入を防ぐ）。
        """
        lines = [f"対象期間: {w_start}〜{w_end}", ""]

        if backlog_acts:
            lines.append("【Backlog活動】")
            seen = set()
            for act in backlog_acts:
                key = act.get("issue_key", "")
                date = (act.get("updated") or "")[:10]
                if key not in seen:
                    seen.add(key)
                    lines.append(
                        f"- [{date}] {key} {act.get('summary','')}（{act.get('status','')}）"
                    )
                if act.get("comment_content"):
                    snippet = act["comment_content"][:200].replace("\n", " ")
                    lines.append(f"  コメント: {snippet}")
            lines.append("")

        if slack_msgs:
            lines.append("【Slack発言】")
            for msg in slack_msgs[:15]:
                date = msg["datetime"].strftime("%m/%d %H:%M")
                text = (msg.get("text") or "")[:100].replace("\n", " ")
                lines.append(f"- [{date}] #{msg.get('channel_name','')}: {text}")
            lines.append("")

        if manual_memos:
            lines.append("【手動メモ（担当者入力）】")
            for memo in manual_memos:
                created = (memo.get("created_at") or "")
                if hasattr(created, "strftime"):
                    date_str = created.strftime("%m/%d %H:%M")
                else:
                    date_str = str(created)[:16]
                lines.append(f"- [{date_str}] {memo.get('text', '')}")
            lines.append("")

        if meeting_docs:
            lines.append("【議事録】")
            for doc in meeting_docs:
                lines.append(f"- [{doc['created_date']}] {doc['title']}")
                summary = doc.get("_summary") or ""
                if summary:
                    filtered = self._filter_summary_lines(summary, issue_keywords)
                    if filtered:
                        lines.append(f"  要約（関連抜粋）: {filtered[:800].replace(chr(10), ' ')}")
                    else:
                        # キーワード一致なし → 要約全体を渡してGeminiに判断させる
                        lines.append(f"  要約: {summary[:800].replace(chr(10), ' ')}")
                else:
                    excerpt = (doc.get("text") or "")[:600].replace("\n", " ")
                    if excerpt:
                        lines.append(f"  内容: {excerpt}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _filter_summary_lines(summary: str, keywords: list[str] = None) -> str:
        """議事録要約をキーワードで行フィルタし、関連行のみ返す。
        キーワード未指定の場合は全文をそのまま返す。
        セクションヘッダ行（##で始まる行）はキーワード関連行があれば一緒に含める。
        """
        if not keywords:
            return summary
        kws = [k.lower() for k in keywords]
        result_lines = []
        current_section_header = ""
        section_has_match = False
        pending_header = ""

        for line in summary.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                # 新セクション開始
                current_section_header = line
                pending_header = line
                section_has_match = False
                continue
            # キーワード一致判定
            if any(kw in stripped.lower() for kw in kws):
                if pending_header:
                    result_lines.append(pending_header)
                    pending_header = ""
                result_lines.append(line)
                section_has_match = True
            # テーブル行（|で始まる）は直前の一致行がある場合のみ追加
            elif stripped.startswith("|") and section_has_match:
                result_lines.append(line)

        return "\n".join(result_lines)

    def _rule_based_status(
        self,
        backlog_acts: list[dict],
        slack_msgs: list[dict],
        meeting_docs: list[dict],
    ) -> str:
        """Gemini 無効時のルールベース フォーマット生成"""
        CLOSED_STATUSES = ["完了", "クローズ", "Done", "Closed", "処理済み"]

        system_items = []
        seen = set()

        for act in backlog_acts:
            key = act.get("issue_key", "")
            if key in seen:
                continue
            seen.add(key)
            system_items.append(
                f"・{key} {act.get('summary','')}（{act.get('status','')}）"
            )

        for msg in slack_msgs[:5]:
            text = (msg.get("text") or "")[:60].replace("\n", " ")
            system_items.append(f"・#{msg.get('channel_name','')}: {text}")

        for doc in meeting_docs:
            system_items.append(f"・会議: {doc['title']}")

        progress_section = (
            "■現在の進捗\n" + "\n".join(system_items)
            if system_items
            else "■現在の進捗\n・対象期間中の活動なし"
        )
        risk_section = "■リスク共有\n特になし"

        return "\n\n".join([progress_section, risk_section])

    def build_daily_summary(self, aggregated: dict) -> str:
        """日次夕方サマリー用テキスト生成（Slack DM送信用・Slackマークダウン形式）"""
        today_str = aggregated["week_start"].strftime("%Y/%m/%d")
        lines = [f"📊 *本日の活動サマリー（{today_str}）*", ""]

        # Backlog
        lines.append("*📋 本日更新した Backlog チケット*")
        if aggregated["backlog_by_project"]:
            for proj, issues in aggregated["backlog_by_project"].items():
                lines.append(f"　【{proj}】")
                for issue_label, activities in issues.items():
                    statuses = list({a["status"] for a in activities if a.get("status")})
                    status_str = f"（{', '.join(statuses)}）" if statuses else ""
                    lines.append(f"　　• {issue_label} {status_str}")
        else:
            lines.append("　（なし）")
        lines.append("")

        # Slack
        total_msgs = sum(len(v) for v in aggregated["slack_by_channel"].values())
        lines.append(f"*💬 本日の Slack 発言（{total_msgs}件）*")
        if aggregated["slack_by_channel"]:
            for channel, msgs in aggregated["slack_by_channel"].items():
                lines.append(f"　#{channel}（{len(msgs)}件）")
                for msg in sorted(msgs, key=lambda x: x["datetime"])[:3]:
                    text = " ".join(msg["text"].replace("\n", " ").split())
                    if len(text) > 60:
                        text = text[:60] + "…"
                    time_str = msg["datetime"].strftime("%H:%M")
                    lines.append(f"　　• `{time_str}` {text}")
        else:
            lines.append("　（なし）")
        lines.append("")
        lines.append("_自動生成_")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  ローカルレポートファイル生成
    # ------------------------------------------------------------------ #

    def save_local_report(self, aggregated: dict, comment_text: str, output_dir: str) -> str:
        """ローカルにMarkdownレポートを保存"""
        import os
        os.makedirs(output_dir, exist_ok=True)
        w_start = aggregated["week_start"].strftime("%Y%m%d")
        filename = f"{output_dir}/weekly_report_{w_start}.md"

        with open(filename, "w", encoding="utf-8") as f:
            f.write(comment_text)

        logger.info(f"ローカルレポート保存: {filename}")
        return filename

    def save_calendar_report(self, calendar_events: list[dict], output_dir: str) -> str:
            """カレンダー情報を日付ごとに別ファイルで出力"""
            import os
            os.makedirs(output_dir, exist_ok=True)

            if not calendar_events:
                return ""

            # 日付ごとにグループ化
            by_day = defaultdict(list)
            for event in calendar_events:
                day = event["start_dt"].strftime("%Y/%m/%d(%a)")
                by_day[day].append(event)

            # 最初のイベントの日付から週を特定
            first_date = calendar_events[0]["start_dt"].strftime("%Y%m%d")
            filename = f"{output_dir}/calendar_report_{first_date}.md"

            lines = []
            total_hours = sum(e["duration_hours"] for e in calendar_events)

            lines.append("# 週間カレンダー・工数レポート")
            lines.append("")
            lines.append(f"**週間合計工数: {total_hours:.1f}時間**")
            lines.append("")
            lines.append("---")
            lines.append("")

            for day in sorted(by_day.keys()):
                events = by_day[day]
                day_total = sum(e["duration_hours"] for e in events)
                lines.append(f"## {day}（合計 {day_total:.1f}h）")
                lines.append("")
                lines.append("| 時間 | イベント名 | 工数 |")
                lines.append("|---|---|---|")
                for ev in sorted(events, key=lambda x: x["start_dt"]):
                    time_str = ev["start_dt"].strftime("%H:%M")
                    lines.append(f"| {time_str} | {ev['summary']} | {ev['duration_hours']}h |")
                lines.append("")

            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            logger.info(f"カレンダーレポート保存: {filename}")
            return filename