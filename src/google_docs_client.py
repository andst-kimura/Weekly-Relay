"""
Google Drive / Docs API クライアント
Meet Recordings フォルダから Gemini 生成議事録を取得する
"""
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from src.google_calendar_client import SCOPES

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_MIME_GDOC = "application/vnd.google-apps.document"


class GoogleDocsClient:
    def __init__(self, credentials_file: str, token_file: str = "config/google_token.pickle"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._drive = None
        self._docs = None
        self._build_services()

    def _build_services(self) -> None:
        import pickle
        creds = None
        token_path = Path(self.token_file)
        if token_path.exists():
            with open(token_path, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)

        self._drive = build("drive", "v3", credentials=creds)
        self._docs = build("docs", "v1", credentials=creds)
        logger.info("Google Drive / Docs API 初期化完了")

    def get_meeting_docs(self, folder_id: str, since: datetime, until: datetime) -> list[dict]:
        """
        指定フォルダ内で対象期間に作成された Google Docs を返す。
        戻り値: [{"id": ..., "title": ..., "created_date": date, "text": ...}, ...]
        """
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        until_utc = until.astimezone(timezone.utc) if until.tzinfo else until.replace(tzinfo=timezone.utc)

        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_str = until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        query = (
            f"'{folder_id}' in parents"
            f" and mimeType = '{_MIME_GDOC}'"
            f" and createdTime >= '{since_str}'"
            f" and createdTime <= '{until_str}'"
            " and trashed = false"
        )

        results = []
        page_token = None
        while True:
            resp = self._drive.files().list(
                q=query,
                fields="nextPageToken, files(id, name, createdTime)",
                orderBy="createdTime",
                pageToken=page_token,
            ).execute()

            for f in resp.get("files", []):
                doc_id = f["id"]
                title = f["name"]
                created_dt = datetime.fromisoformat(
                    f["createdTime"].replace("Z", "+00:00")
                ).astimezone(JST)
                try:
                    text = self._extract_text(doc_id)
                except Exception as e:
                    logger.warning(f"議事録本文取得失敗 ({title}): {e}")
                    text = ""
                results.append({
                    "id": doc_id,
                    "title": title,
                    "created_date": created_dt.date(),
                    "created_datetime": created_dt,
                    "text": text,
                })
                logger.info(f"議事録取得: {title} ({created_dt.strftime('%Y/%m/%d')})")

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"議事録 {len(results)} 件取得完了")
        return results

    def get_docs_from_events(self, calendar_events: list[dict]) -> list[dict]:
        """
        カレンダーイベントの attachments から Gemini 議事録を取得する。
        自分がオーナーでない会議の議事録も含む（参加した全 MTG が対象）。
        戻り値は get_meeting_docs と同じ形式。
        """
        results = []
        seen_ids: set[str] = set()

        for event in calendar_events:
            for attachment in event.get("attachments", []):
                # fileId が取得できない場合は alternateLink から抽出
                doc_id = attachment.get("fileId") or self._extract_doc_id(
                    attachment.get("fileUrl", "")
                )
                if not doc_id or doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                attach_title = attachment.get("title", "")
                event_date = event["start_dt"].date()

                try:
                    doc_title, text = self._get_doc(doc_id)
                    # Docs API から実際のタイトルを取得できた場合はそちらを優先
                    title = doc_title or attach_title
                except Exception as e:
                    logger.warning(f"議事録本文取得失敗（カレンダー添付）({attach_title}): {e}")
                    # 403 等でアクセス不可のドキュメントはスキップ（権限なし）
                    continue

                results.append({
                    "id": doc_id,
                    "title": title,
                    "created_date": event_date,
                    "created_datetime": event["start_dt"],
                    "text": text,
                    "source": "calendar_attachment",
                    "event_summary": event.get("summary", ""),
                })
                logger.info(
                    f"議事録取得（カレンダー添付）: {title} "
                    f"[{event.get('summary','')}] ({event_date})"
                )

        logger.info(f"カレンダー添付から議事録 {len(results)} 件取得完了")
        return results

    @staticmethod
    def _extract_doc_id(url: str) -> str:
        """Google Docs URL からドキュメントIDを抽出する"""
        # https://docs.google.com/document/d/DOC_ID/edit
        import re
        m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
        return m.group(1) if m else ""

    def merge_docs(self, *doc_lists: list[dict]) -> list[dict]:
        """複数ソースの議事録をドキュメントIDで重複排除してマージする"""
        seen: set[str] = set()
        merged = []
        for docs in doc_lists:
            for doc in docs:
                doc_id = doc.get("id", "")
                if doc_id and doc_id not in seen:
                    seen.add(doc_id)
                    merged.append(doc)
        merged.sort(key=lambda d: d["created_date"])
        return merged

    def _get_doc(self, doc_id: str) -> tuple[str, str]:
        """Google Docs のタイトルと本文テキストを取得する"""
        doc = self._docs.documents().get(documentId=doc_id).execute()
        title = doc.get("title", "")
        parts = []
        for element in doc.get("body", {}).get("content", []):
            para = element.get("paragraph")
            if not para:
                continue
            for pe in para.get("elements", []):
                run = pe.get("textRun")
                if run:
                    parts.append(run.get("content", ""))
        return title, "".join(parts).strip()

    def _extract_text(self, doc_id: str) -> str:
        """Google Docs の本文テキストを抽出する"""
        _, text = self._get_doc(doc_id)
        return text
