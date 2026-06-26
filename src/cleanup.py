"""
クリーンアップモジュール
Wasabi が転記したコメント・課題を対話形式で削除する
"""
import logging
from datetime import datetime, timezone, timedelta

from src.backlog_client import BacklogClient

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# Wasabi が投稿したコメントを識別するフッター文字列
# "Weekly Relay" は旧名称（リネーム前）のため両方を検出対象とする
_WR_SIGNATURES = [
    "Wasabi により自動転記されました",
    "Weekly Relay により自動転記されました",
]


class CleanupTool:
    def __init__(self, client: BacklogClient, report_project_key: str):
        self.client = client
        self.report_project_key = report_project_key

    def run(self) -> None:
        """対話形式のクリーンアップメニューを起動する"""
        print("\n" + "=" * 60)
        print("  Wasabi クリーンアップツール")
        print("=" * 60)
        print("Wasabi が転記したコメント・課題を削除します。\n")

        while True:
            print("操作を選んでください:")
            print("  1. コメントを削除（親課題へのコメント転記分）")
            print("  2. 課題を削除（Wasabi が起票した課題）")
            print("  q. 終了")
            choice = input("\n> ").strip().lower()

            if choice == "1":
                self._cleanup_comments()
            elif choice == "2":
                self._cleanup_issues()
            elif choice == "q":
                print("終了します。")
                break
            else:
                print("1 / 2 / q を入力してください。\n")

    # ------------------------------------------------------------------ #
    #  コメント削除
    # ------------------------------------------------------------------ #

    def _cleanup_comments(self) -> None:
        """Wasabi のコメントを検索して対話形式で削除する"""
        print("\nプロジェクトの親課題を検索中...")
        try:
            parent_issues = self.client.get_parent_issues(self.report_project_key)
        except Exception as e:
            print(f"[ERROR] 親課題の取得に失敗しました: {e}")
            return

        found: list[dict] = []
        for issue in parent_issues:
            issue_key = issue.get("issueKey", "")
            issue_id = issue.get("id")
            summary = issue.get("summary", "")
            try:
                comments = self.client.get_all_comments(issue_id)
            except Exception as e:
                logger.warning(f"コメント取得失敗 {issue_key}: {e}")
                continue

            for comment in comments:
                content = comment.get("content") or ""
                if not any(sig in content for sig in _WR_SIGNATURES):
                    continue
                created_raw = comment.get("created", "")
                try:
                    created_dt = datetime.fromisoformat(
                        created_raw.replace("Z", "+00:00")
                    ).astimezone(JST).strftime("%Y/%m/%d %H:%M")
                except Exception:
                    created_dt = created_raw[:16]

                # コメント冒頭を最大60文字で抜粋
                excerpt = content.replace("\n", " ")[:60]

                found.append({
                    "index": len(found) + 1,
                    "issue_key": issue_key,
                    "issue_summary": summary,
                    "comment_id": comment["id"],
                    "created": created_dt,
                    "excerpt": excerpt,
                })

        if not found:
            print("\nWasabi のコメントは見つかりませんでした。\n")
            return

        print(f"\n{len(found)} 件の Wasabi コメントが見つかりました:\n")
        for item in found:
            print(
                f"  [{item['index']:>2}] {item['issue_key']} 「{item['issue_summary'][:20]}」"
                f"  {item['created']}"
            )
            print(f"       {item['excerpt']}...")
            print()

        targets = self._parse_selection(found)
        if not targets:
            return

        print(f"\n以下の {len(targets)} 件を削除します:")
        for t in targets:
            print(f"  - [{t['index']}] {t['issue_key']} コメント#{t['comment_id']}  {t['created']}")

        if not self._confirm():
            print("キャンセルしました。\n")
            return

        deleted = 0
        for t in targets:
            try:
                self.client.delete_comment(t["issue_key"], t["comment_id"])
                print(f"  ✅ 削除: {t['issue_key']} コメント#{t['comment_id']}")
                deleted += 1
            except Exception as e:
                print(f"  ❌ 削除失敗: {t['issue_key']} コメント#{t['comment_id']} — {e}")

        print(f"\n{deleted} 件削除しました。\n")

    # ------------------------------------------------------------------ #
    #  課題削除
    # ------------------------------------------------------------------ #

    def _cleanup_issues(self) -> None:
        """Wasabi が起票した課題を検索して対話形式で削除する"""
        keyword = input(
            "\n削除対象の課題キーを入力してください（カンマ区切り / q でキャンセル）\n"
            "例: SALES_TEAM-999,SALES_TEAM-1000\n> "
        ).strip()

        if keyword.lower() == "q" or not keyword:
            print("キャンセルしました。\n")
            return

        keys = [k.strip() for k in keyword.split(",") if k.strip()]
        found: list[dict] = []

        for key in keys:
            try:
                issue = self.client.get_issue(key)
                found.append({
                    "index": len(found) + 1,
                    "issue_key": issue.get("issueKey", key),
                    "summary": issue.get("summary", ""),
                    "status": issue.get("status", {}).get("name", ""),
                    "created": issue.get("created", "")[:10],
                })
            except Exception as e:
                print(f"  [WARNING] {key} の取得失敗（スキップ）: {e}")

        if not found:
            print("削除対象の課題が見つかりませんでした。\n")
            return

        print(f"\n{len(found)} 件の課題が見つかりました:\n")
        for item in found:
            print(
                f"  [{item['index']:>2}] {item['issue_key']}"
                f"  [{item['status']}]  {item['summary'][:40]}"
                f"  （作成: {item['created']}）"
            )
        print()

        targets = self._parse_selection(found)
        if not targets:
            return

        print(f"\n以下の {len(targets)} 件の課題を削除します（元に戻せません）:")
        for t in targets:
            print(f"  - [{t['index']}] {t['issue_key']}  {t['summary'][:40]}")

        if not self._confirm(danger=True):
            print("キャンセルしました。\n")
            return

        deleted = 0
        for t in targets:
            try:
                self.client.delete_issue(t["issue_key"])
                print(f"  ✅ 削除: {t['issue_key']}  {t['summary'][:40]}")
                deleted += 1
            except Exception as e:
                print(f"  ❌ 削除失敗: {t['issue_key']} — {e}")

        print(f"\n{deleted} 件削除しました。\n")

    # ------------------------------------------------------------------ #
    #  ユーティリティ
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_selection(items: list[dict]) -> list[dict]:
        """
        ユーザー入力（番号 / all / q）を解析して対象アイテムリストを返す。
        キャンセル時は空リストを返す。
        """
        print("削除する番号を入力してください（カンマ区切り / all / q でキャンセル）")
        raw = input("> ").strip().lower()

        if raw == "q" or not raw:
            print("キャンセルしました。\n")
            return []

        if raw == "all":
            return items

        selected = []
        index_map = {item["index"]: item for item in items}
        for token in raw.split(","):
            token = token.strip()
            if not token.isdigit():
                print(f"[WARN] '{token}' は無効な入力です（スキップ）")
                continue
            idx = int(token)
            if idx not in index_map:
                print(f"[WARN] 番号 {idx} は存在しません（スキップ）")
                continue
            selected.append(index_map[idx])

        return selected

    @staticmethod
    def _confirm(danger: bool = False) -> bool:
        """削除実行前の最終確認プロンプト"""
        if danger:
            prompt = "本当に削除しますか？この操作は元に戻せません。（yes / n）: "
            return input(prompt).strip().lower() == "yes"
        return input("削除を実行しますか？（y / n）: ").strip().lower() == "y"
