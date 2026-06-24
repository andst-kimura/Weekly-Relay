"""
Backlog転記モジュール
親課題へのコメント追加、または新規課題作成を行う
"""
import logging
from collections import defaultdict
from src.backlog_client import BacklogClient

logger = logging.getLogger(__name__)

CLOSED_STATUSES = ["完了", "クローズ", "Done", "Closed", "処理済み"]


class BacklogPoster:
    def __init__(self, client: BacklogClient, report_project_key: str,
                 channel_mapping: dict = None, dry_run: bool = False):
        self.client = client
        self.report_project_key = report_project_key
        self.channel_mapping = channel_mapping or {}
        self.dry_run = dry_run
        self._report_project = None
        self._issue_types = None

    def _get_report_project(self) -> dict:
        if self._report_project is None:
            self._report_project = self.client.get_project(self.report_project_key)
        return self._report_project

    def _get_issue_types(self) -> list[dict]:
        if self._issue_types is None:
            proj = self._get_report_project()
            self._issue_types = self.client.get_issue_types(proj["id"])
        return self._issue_types

    def _get_default_issue_type_id(self) -> int:
        types = self._get_issue_types()
        return types[0]["id"] if types else None

    def _post_comment(self, issue_key: str, content: str) -> dict:
        try:
            issue = self.client.get_issue(issue_key)
            issue_id = issue["id"]
            if self.dry_run:
                logger.info(f"[DRY RUN] コメント転記スキップ: {issue_key}")
                return {"action": "comment_skipped_dry_run", "issue_key": issue_key}
            self.client.add_comment_to_issue(issue_id, content)
            logger.info(f"✅ コメント転記完了: {issue_key}")
            return {"action": "commented", "issue_key": issue_key}
        except Exception as e:
            logger.error(f"コメント転記失敗 {issue_key}: {e}")
            return {"action": "error", "issue_key": issue_key, "error": str(e)}

    def _create_issue(self, summary: str, description: str) -> dict:
        try:
            proj = self._get_report_project()
            issue_type_id = self._get_default_issue_type_id()
            if self.dry_run:
                logger.info(f"[DRY RUN] 新規起票スキップ: {summary}")
                return {"action": "create_skipped_dry_run", "summary": summary}
            new_issue = self.client.create_issue(
                project_id=proj["id"],
                summary=summary,
                description=description,
                issue_type_id=issue_type_id,
            )
            issue_key = new_issue.get("issueKey", "")
            logger.info(f"✅ 新規起票完了: {issue_key} {summary}")
            return {"action": "created_issue", "issue_key": issue_key}
        except Exception as e:
            logger.error(f"新規起票失敗 {summary}: {e}")
            return {"action": "error", "summary": summary, "error": str(e)}

    def _is_closed(self, issue: dict) -> bool:
        return issue.get("status", {}).get("name", "") in CLOSED_STATUSES

    def _build_comment(self, issue_key: str, issue_summary: str,
                        backlog_acts: list, slack_msgs: list, meeting_docs: list,
                        aggregated: dict, generator, gemini_client,
                        tag: str = "") -> str:
        """build_status_next_action を呼び出してコメント本文を返す"""
        footer_extra = f"（{tag}）" if tag else ""
        body = generator.build_status_next_action(
            backlog_acts=backlog_acts,
            slack_msgs=slack_msgs,
            meeting_docs=meeting_docs,
            week_start=aggregated["week_start"],
            week_end=aggregated["week_end"],
            issue_summary=f"{issue_key}: {issue_summary}",
            gemini_client=gemini_client,
        )
        # 末尾のフッターにタグを追加
        if tag:
            body = body.replace(
                "*このコメントは Weekly Relay により自動転記されました*",
                f"*このコメントは Weekly Relay により自動転記されました{footer_extra}*",
            )
        return body

    def post_weekly_report(self, comment_text: str, backlog_activities: list[dict],
                            slack_messages: list[dict], week_start, week_end,
                            aggregated: dict = None, generator=None,
                            meeting_docs: list[dict] = None,
                            gemini_client=None) -> list[dict]:
        from src.gemini_client import is_empty_meeting_doc

        results = []
        posted_issue_keys: set[str] = set()

        # 有効な議事録のみ使用（Gemini生成失敗の空ドキュメントを除外）
        valid_meeting_docs = [d for d in (meeting_docs or []) if not is_empty_meeting_doc(d)]
        if meeting_docs and len(valid_meeting_docs) < len(meeting_docs):
            skipped = len(meeting_docs) - len(valid_meeting_docs)
            logger.info(f"議事録フィルタリング: {skipped} 件をスキップ（内容未生成）")

        # Slack発言をチャンネルごとに整理
        slack_by_channel: dict[str, list] = defaultdict(list)
        for msg in slack_messages:
            slack_by_channel[msg["channel_name"]].append(msg)

        # ------------------------------------------------------------------ #
        # ① channel_mapping で明示指定された親課題に転記
        # ------------------------------------------------------------------ #
        for channel, mapping in self.channel_mapping.items():
            parent_key = mapping.get("parent_issue_key", "")
            label = mapping.get("label", channel)
            msgs = slack_by_channel.get(channel, [])

            if not msgs:
                logger.info(f"スキップ（発言なし）: #{channel}")
                continue
            if not parent_key:
                logger.info(f"スキップ（親課題未指定）: #{channel} [{label}]")
                continue
            if parent_key in posted_issue_keys:
                logger.info(f"スキップ（転記済み）: {parent_key}")
                continue

            try:
                parent_info = self.client.get_issue(parent_key)
            except Exception as e:
                logger.warning(f"親課題取得失敗 {parent_key}: {e}")
                continue

            if self._is_closed(parent_info):
                logger.info(f"転記スキップ（クローズ済み）: {parent_key}")
                continue

            parent_summary = parent_info.get("summary", "")
            project_keys = [pk.strip() for pk in mapping.get("project_key", "").split(",") if pk.strip()]
            related_channels = [
                ch for ch, m in self.channel_mapping.items()
                if m.get("parent_issue_key") == parent_key
            ]

            rel_acts = [
                a for a in backlog_activities
                if a.get("project_key") in project_keys
            ] if project_keys else []
            rel_msgs = []
            for rc in related_channels:
                rel_msgs.extend(slack_by_channel.get(rc, []))

            # この親課題に関連する議事録のみ渡す（Geminiで個別分類）
            rel_docs = self._classify_docs_for_issue(
                valid_meeting_docs, parent_key, parent_summary, gemini_client
            )

            logger.info(f"#{channel} → {parent_key} に転記（関連PJ: {project_keys}, 議事録: {len(rel_docs)} 件）")
            comment = self._build_comment(
                parent_key, parent_summary,
                rel_acts, rel_msgs, rel_docs,
                aggregated, generator, gemini_client,
            )
            results.append(self._post_comment(parent_key, comment))
            posted_issue_keys.add(parent_key)

        # ------------------------------------------------------------------ #
        # ② Backlog 活動を親課題チェーンで確定的にマッピング
        #    （SALES_TEAM 親課題まで API を遡って判定 → Gemini 推測不要）
        # ------------------------------------------------------------------ #
        # 課題ごとに SALES_TEAM 親課題を解決してマッピング
        act_by_parent: dict[str, list] = defaultdict(list)
        for act in backlog_activities:
            issue_key = act.get("issue_key", "")
            if not issue_key:
                continue
            sales_parent_key = self._resolve_sales_parent(issue_key, act)
            if sales_parent_key:
                act_by_parent[sales_parent_key].append(act)

        for parent_key, acts in act_by_parent.items():
            if parent_key in posted_issue_keys:
                logger.info(f"転記スキップ（転記済み）: {parent_key}")
                continue
            try:
                parent_issue = self.client.get_issue(parent_key)
            except Exception as e:
                logger.error(f"親課題取得失敗 {parent_key}: {e}")
                continue
            if self._is_closed(parent_issue):
                logger.info(f"転記スキップ（クローズ済み）: {parent_key}")
                continue

            rel_docs = self._classify_docs_for_issue(
                valid_meeting_docs, parent_key, parent_issue.get("summary", ""), gemini_client
            )
            logger.info(f"Backlog活動を {parent_key} に転記（課題 {len(acts)} 件, 議事録 {len(rel_docs)} 件）")
            comment = self._build_comment(
                parent_key, parent_issue.get("summary", ""),
                acts, [], rel_docs,
                aggregated, generator, gemini_client,
            )
            results.append(self._post_comment(parent_key, comment))
            posted_issue_keys.add(parent_key)

        # ------------------------------------------------------------------ #
        # ③ Gemini による親課題判別（Slack未マッピング分・議事録残分）
        # ------------------------------------------------------------------ #
        if gemini_client and gemini_client.enabled:
            results += self._post_gemini_detected(
                slack_by_channel=dict(slack_by_channel),
                meeting_docs=valid_meeting_docs,
                aggregated=aggregated,
                generator=generator,
                gemini_client=gemini_client,
                already_posted=posted_issue_keys,
            )

        return results

    # ------------------------------------------------------------------ #
    #  親課題チェーン解決（確定的マッピング）
    # ------------------------------------------------------------------ #

    def _resolve_sales_parent(self, issue_key: str, act: dict) -> str | None:
        """
        Backlog 活動の課題から SALES_TEAM 親課題キーを確定的に解決する。
        ① act に parent_issue_id があれば API で遡る
        ② なければ issue_key 自体が SALES_TEAM の子課題か確認
        """
        sales_prefix = f"{self.report_project_key}-"
        parent_id = act.get("parent_issue_id")

        if parent_id:
            resolved = self.client.resolve_sales_team_parent(
                parent_id, self.report_project_key
            )
            if resolved:
                return resolved

        # issue_key 自体が SALES_TEAM プロジェクトの子課題の場合
        if issue_key.startswith(sales_prefix):
            resolved = self.client.resolve_sales_team_parent(
                issue_key, self.report_project_key
            )
            return resolved

        return None

    # ------------------------------------------------------------------ #
    #  議事録を親課題ごとに個別分類（Gemini）
    # ------------------------------------------------------------------ #

    def _classify_docs_for_issue(
        self,
        meeting_docs: list[dict],
        issue_key: str,
        issue_summary: str,
        gemini_client,
    ) -> list[dict]:
        """
        各議事録ドキュメントを個別に Gemini で分類し、
        指定した親課題に関連するものだけを返す。
        Gemini が無効の場合は全件返す（従来動作）。
        """
        if not gemini_client or not gemini_client.enabled or not meeting_docs:
            return meeting_docs

        related = []
        parent_issues = [{"issue_key": issue_key, "summary": issue_summary}]
        for doc in meeting_docs:
            date_str = doc["created_date"].strftime("%Y/%m/%d")
            excerpt = (doc.get("text") or "")[:500].replace("\n", " ")
            content = (
                f"議事録タイトル: {doc['title']}\n"
                f"日時: {date_str}\n"
                f"内容: {excerpt}"
            )
            matched = gemini_client.detect_parent_issue(content, parent_issues)
            if matched == issue_key:
                related.append(doc)
            else:
                logger.debug(f"議事録スキップ（{issue_key}と無関係）: {doc['title']}")
        return related

    # ------------------------------------------------------------------ #
    #  Gemini 親課題判別（Slack未マッピング分のみ）
    # ------------------------------------------------------------------ #

    def _post_gemini_detected(
        self,
        slack_by_channel: dict,
        meeting_docs: list[dict],
        aggregated: dict,
        generator,
        gemini_client,
        already_posted: set,
    ) -> list[dict]:
        """
        channel_mapping 未登録の Slack チャンネルを Gemini で分類して転記する。
        Backlog 活動は ② で確定的に処理済みのためここでは対象外。
        """
        try:
            raw_parents = self.client.get_parent_issues(
                self.report_project_key, exclude_statuses=CLOSED_STATUSES
            )
        except Exception as e:
            logger.warning(f"親課題一覧の取得失敗（Gemini判別をスキップ）: {e}")
            return []

        parent_issues_for_gemini = [
            {"issue_key": p.get("issueKey", ""), "summary": p.get("summary", "")}
            for p in raw_parents if p.get("issueKey")
        ]
        if not parent_issues_for_gemini:
            logger.info("Gemini判別: 有効な親課題が見つかりませんでした")
            return []

        logger.info(f"Gemini判別対象の親課題: {len(parent_issues_for_gemini)} 件")

        # 親課題ごとにデータを蓄積
        acc: dict[str, dict] = defaultdict(lambda: {"slack_msgs": [], "meeting_docs": []})

        mapped_channels = set(self.channel_mapping.keys())

        # channel_mapping 未登録チャンネルのみ Gemini 判別
        for channel, msgs in slack_by_channel.items():
            if channel in mapped_channels or not msgs:
                continue
            texts = " / ".join(
                (m.get("text") or "")[:80].replace("\n", " ") for m in msgs[:5]
            )
            content = f"Slackチャンネル: #{channel}\n発言サンプル: {texts}"
            key = gemini_client.detect_parent_issue(content, parent_issues_for_gemini)
            if key:
                acc[key]["slack_msgs"].extend(msgs)

        # 蓄積分をフォーマットして転記
        results = []
        parent_summary_map = {p["issue_key"]: p["summary"] for p in parent_issues_for_gemini}

        for issue_key, data in acc.items():
            if issue_key in already_posted:
                logger.info(f"Gemini判別: {issue_key} は転記済みのためスキップ")
                continue
            if not data["slack_msgs"]:
                continue
            try:
                parent = self.client.get_issue(issue_key)
                if self._is_closed(parent):
                    logger.info(f"Gemini判別: {issue_key} はクローズ済みのためスキップ")
                    continue
            except Exception as e:
                logger.warning(f"Gemini判別: {issue_key} のステータス確認失敗: {e}")

            parent_summary = parent_summary_map.get(issue_key, "")
            # この親課題に関連する議事録を個別分類して取得
            rel_docs = self._classify_docs_for_issue(
                meeting_docs, issue_key, parent_summary, gemini_client
            )
            total = len(data["slack_msgs"]) + len(rel_docs)
            logger.info(f"Gemini判別: {issue_key} に転記（Slack {len(data['slack_msgs'])} 件, 議事録 {len(rel_docs)} 件）")
            comment = self._build_comment(
                issue_key, parent_summary,
                [], data["slack_msgs"], rel_docs,
                aggregated, generator, gemini_client,
                tag="Gemini判別",
            )
            result = self._post_comment(issue_key, comment)
            result["detected_by"] = "gemini"
            results.append(result)
            already_posted.add(issue_key)

        return results
