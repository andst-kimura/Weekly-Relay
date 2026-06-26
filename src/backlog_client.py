"""
Backlog API クライアント
自分が関わった課題・コメント・操作履歴を取得する
"""
import requests
from requests.adapters import HTTPAdapter
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class BacklogClient:
    def __init__(self, base_url: str, api_key: str, my_user_id: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.my_user_id = my_user_id
        self.session = requests.Session()
        # KB生成時に workers=8 × 2内部スレッド = 最大16接続を張るため上限を引き上げる
        adapter = HTTPAdapter(pool_maxsize=20, pool_connections=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """レートリミット・一時エラー時にリトライするリクエスト共通処理"""
        for attempt in range(_MAX_RETRIES):
            response = self.session.request(method, url, **kwargs)
            if response.status_code not in _RETRY_STATUS_CODES:
                response.raise_for_status()
                return response
            wait = int(response.headers.get("Retry-After", 2 ** attempt))
            logger.warning(f"Backlog API {response.status_code}: {wait}秒後にリトライ ({attempt + 1}/{_MAX_RETRIES})")
            time.sleep(wait)
        response.raise_for_status()
        return response

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        params = params or {}
        params["apiKey"] = self.api_key
        url = f"{self.base_url}/api/v2/{endpoint}"
        return self._request_with_retry("GET", url, params=params).json()

    def _patch(self, endpoint: str, data: dict) -> dict:
        url = f"{self.base_url}/api/v2/{endpoint}"
        return self._request_with_retry("PATCH", url, params={"apiKey": self.api_key}, json=data).json()

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f"{self.base_url}/api/v2/{endpoint}"
        return self._request_with_retry("POST", url, params={"apiKey": self.api_key}, json=data).json()

    def get_my_user_id(self) -> int:
        """自分のユーザーIDを確認する"""
        user = self._get("users/myself")
        return user["id"]

    def get_my_projects(self) -> list[dict]:
        """参加している全プロジェクトを取得"""
        return self._get("projects")

    def get_issues_assigned_to_me(self, project_id: int, since: datetime, until: datetime) -> list[dict]:
        """自分が担当者の課題を取得"""
        params = {
            "projectId[]": project_id,
            "assigneeId[]": self.my_user_id,
            "updatedSince": since.strftime("%Y-%m-%d"),
            "updatedUntil": until.strftime("%Y-%m-%d"),
            "count": 100,
        }
        return self._get("issues", params)

    def get_issues_created_by_me(self, project_id: int, since: datetime, until: datetime) -> list[dict]:
        """自分が作成した課題を取得"""
        params = {
            "projectId[]": project_id,
            "createdUserId[]": self.my_user_id,
            "createdSince": since.strftime("%Y-%m-%d"),
            "createdUntil": until.strftime("%Y-%m-%d"),
            "count": 100,
        }
        return self._get("issues", params)

    def get_my_comments_in_project(self, project_id: int, since: datetime, until: datetime) -> list[dict]:
        """プロジェクト内で自分がコメントした課題とコメント内容を取得"""
        # まずプロジェクト内の全課題を取得し、各課題のコメントから自分のものを抽出
        params = {
            "projectId[]": project_id,
            "count": 100,
            "updatedSince": since.strftime("%Y-%m-%d"),
            "updatedUntil": until.strftime("%Y-%m-%d"),
        }
        issues = self._get("issues", params)
        my_comments = []

        for issue in issues:
            comments = self._get(f"issues/{issue['id']}/comments", {"count": 100})
            for comment in comments:
                if comment.get("createdUser", {}).get("id") == self.my_user_id:
                    created = datetime.fromisoformat(comment["created"].replace("Z", "+00:00"))
                    if since.replace(tzinfo=created.tzinfo) <= created <= until.replace(tzinfo=created.tzinfo):
                        my_comments.append({
                            "issue": issue,
                            "comment": comment,
                        })
        return my_comments

    def _fetch_project_activities(self, project: dict, since: datetime, until: datetime) -> list[dict]:
        """1プロジェクト分の活動を取得（並列実行用）"""
        project_id = project["id"]
        project_name = project["name"]
        project_key = project["projectKey"]
        logger.info(f"Backlog: プロジェクト '{project_name}' を処理中...")
        activities = []
        try:
            assigned = self.get_issues_assigned_to_me(project_id, since, until)
            for issue in assigned:
                activities.append({
                    "type": "assigned_issue",
                    "project_name": project_name,
                    "project_key": project_key,
                    "issue_id": issue["id"],
                    "issue_key": issue.get("issueKey", ""),
                    "summary": issue.get("summary", ""),
                    "status": issue.get("status", {}).get("name", ""),
                    "parent_issue_id": issue.get("parentIssueId"),
                    "updated": issue.get("updated", ""),
                    "description": issue.get("description", ""),
                })

            created = self.get_issues_created_by_me(project_id, since, until)
            created_ids = {i["id"] for i in assigned}
            for issue in created:
                if issue["id"] not in created_ids:
                    activities.append({
                        "type": "created_issue",
                        "project_name": project_name,
                        "project_key": project_key,
                        "issue_id": issue["id"],
                        "issue_key": issue.get("issueKey", ""),
                        "summary": issue.get("summary", ""),
                        "status": issue.get("status", {}).get("name", ""),
                        "parent_issue_id": issue.get("parentIssueId"),
                        "updated": issue.get("updated", ""),
                        "description": issue.get("description", ""),
                    })

            my_comments = self.get_my_comments_in_project(project_id, since, until)
            for entry in my_comments:
                activities.append({
                    "type": "comment",
                    "project_name": project_name,
                    "project_key": project_key,
                    "issue_id": entry["issue"]["id"],
                    "issue_key": entry["issue"].get("issueKey", ""),
                    "summary": entry["issue"].get("summary", ""),
                    "status": entry["issue"].get("status", {}).get("name", ""),
                    "parent_issue_id": entry["issue"].get("parentIssueId"),
                    "comment_id": entry["comment"]["id"],
                    "comment_content": entry["comment"].get("content", ""),
                    "updated": entry["comment"].get("created", ""),
                })
        except Exception as e:
            logger.warning(f"プロジェクト '{project_name}' の取得中にエラー: {e}")
        return activities

    def get_all_my_activities(self, since: datetime, until: datetime, target_projects: list[str] = None, exclude_projects: list[str] = None) -> list[dict]:
        """全プロジェクトから自分の活動を並列取得"""
        projects = self.get_my_projects()
        if target_projects:
            projects = [p for p in projects if p["projectKey"] in target_projects]
            logger.info(f"対象プロジェクトを絞り込み: {[p['projectKey'] for p in projects]}")
        if exclude_projects:
            projects = [p for p in projects if p["projectKey"] not in exclude_projects]
            logger.info(f"除外後のプロジェクト: {[p['projectKey'] for p in projects]}")

        all_activities = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self._fetch_project_activities, p, since, until): p for p in projects}
            for future in as_completed(futures):
                all_activities.extend(future.result())

        return all_activities

    def get_all_activities_for_user(self, user_id: int, since: datetime, until: datetime,
                                     target_projects: list[str] = None,
                                     exclude_projects: list[str] = None) -> list[dict]:
        """指定ユーザーの全活動を取得（KB チーム収集用）。自分以外のメンバーにも使用可。"""
        original = self.my_user_id
        self.my_user_id = user_id
        try:
            return self.get_all_my_activities(since, until, target_projects, exclude_projects)
        finally:
            self.my_user_id = original

    def get_all_assigned_issues(self, project_id: int) -> list[dict]:
        """自分が担当者の全課題を取得（日付フィルタなし・全ステータス対象）"""
        all_issues = []
        offset = 0
        while True:
            params = {
                "projectId[]": project_id,
                "assigneeId[]": self.my_user_id,
                "count": 100,
                "offset": offset,
            }
            batch = self._get("issues", params)
            if not batch:
                break
            all_issues.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return all_issues

    def get_all_comments(self, issue_id: int) -> list[dict]:
        """チケットの全コメントを取得（投稿者問わず）"""
        return self._get(f"issues/{issue_id}/comments", {"count": 100})

    def get_issue(self, issue_id_or_key: str) -> dict:
        """課題の詳細を取得"""
        return self._get(f"issues/{issue_id_or_key}")

    def add_comment_to_issue(self, issue_id: int, content: str) -> dict:
        """課題にコメントを追加（進捗報告の転記先）"""
        return self._post(f"issues/{issue_id}/comments", {"content": content})

    def delete_comment(self, issue_id_or_key: str, comment_id: int) -> dict:
        """課題のコメントを削除する"""
        url = f"{self.base_url}/api/v2/issues/{issue_id_or_key}/comments/{comment_id}"
        return self._request_with_retry("DELETE", url, params={"apiKey": self.api_key}).json()

    def delete_issue(self, issue_id_or_key: str) -> dict:
        """課題を削除する"""
        url = f"{self.base_url}/api/v2/issues/{issue_id_or_key}"
        return self._request_with_retry("DELETE", url, params={"apiKey": self.api_key}).json()

    def create_issue(self, project_id: int, summary: str, description: str,
                     issue_type_id: int, priority_id: int = 3,
                     assignee_id: int = None, due_date: str = None) -> dict:
        """新規課題を作成。due_date は 'YYYY-MM-DD' 形式。"""
        data = {
            "projectId": project_id,
            "summary": summary,
            "issueTypeId": issue_type_id,
            "priorityId": priority_id,
            "description": description,
        }
        if assignee_id:
            data["assigneeId"] = assignee_id
        if due_date:
            data["dueDate"] = due_date
        return self._post("issues", data)

    def get_project_users(self, project_id_or_key: str) -> list[dict]:
        """プロジェクトメンバー一覧を取得"""
        return self._get(f"projects/{project_id_or_key}/users")

    def get_issue_types(self, project_id: int) -> list[dict]:
        """プロジェクトの課題種別を取得"""
        return self._get(f"projects/{project_id}/issueTypes")

    def get_project(self, project_key: str) -> dict:
        """プロジェクト情報を取得"""
        return self._get(f"projects/{project_key}")

    def get_parent_issues(self, project_key: str,
                          exclude_statuses: list[str] = None) -> list[dict]:
        """プロジェクトの親課題一覧を取得（子課題を除く）"""
        project = self.get_project(project_key)
        params = {
            "projectId[]": project["id"],
            "parentChild": 1,   # 子課題を除くトップレベル課題のみ
            "count": 100,
        }
        issues = self._get("issues", params)
        if exclude_statuses:
            issues = [i for i in issues
                      if i.get("status", {}).get("name") not in exclude_statuses]
        return issues

    def resolve_sales_team_parent(self, issue_id_or_key: str,
                                  report_project_key: str,
                                  _depth: int = 0) -> str | None:
        """
        課題を起点に親課題チェーンを遡り、report_project_key プロジェクトの
        親課題キー（SALES_TEAM-XXX 等）を返す。見つからなければ None。
        最大5段階まで遡る（無限ループ防止）。
        """
        if _depth > 5:
            return None
        try:
            issue = self.get_issue(str(issue_id_or_key))
        except Exception as e:
            logger.debug(f"課題取得失敗 ({issue_id_or_key}): {e}")
            return None

        issue_key = issue.get("issueKey", "")
        # このプロジェクトの課題で、かつ親課題ID を持たない → 親課題として確定
        if issue_key.startswith(f"{report_project_key}-"):
            parent_id = issue.get("parentIssueId")
            if parent_id is None:
                return issue_key
            # 子課題の場合はさらに遡る
            return self.resolve_sales_team_parent(parent_id, report_project_key, _depth + 1)

        # 別プロジェクト課題 → 親課題IDがあれば遡る
        parent_id = issue.get("parentIssueId")
        if parent_id:
            return self.resolve_sales_team_parent(parent_id, report_project_key, _depth + 1)

        return None
