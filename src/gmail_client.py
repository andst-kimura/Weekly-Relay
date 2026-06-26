"""
Gmail クライアント
Google OAuth2 認証（google_calendar_client.py と共通トークン）を使い、
Gmail REST API でメールを取得する。

主な用途:
  - 今月の請求書メール一覧（Bot コマンド「請求書」）
  - 未返信メールのチェック（Bot コマンド「未返信チェック」）
"""
import base64
import logging
import os
import pickle
from datetime import datetime, timezone, timedelta

import requests as req
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# google_calendar_client.py と同じトークンファイル・スコープを共有
_TOKEN_FILE = "config/google_token.pickle"
_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_MAX_RESULTS = 50


def _authenticate(credentials_file: str):
    creds = None
    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return creds


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(payload: dict) -> str:
    """メール本文（plain text）を取得する"""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace") if data else ""
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _decode_body(part)
            if text:
                return text
    return ""


class GmailClient:
    def __init__(self, credentials_file: str, my_email: str,
                 invoice_senders: list[str] = None, unreplied_days: int = 3):
        self.my_email = my_email
        self.invoice_senders = invoice_senders or []
        self.unreplied_days = unreplied_days
        self._creds = _authenticate(credentials_file)

    def _get(self, path: str, params: dict = None) -> dict:
        self._creds.refresh(Request())
        headers = {"Authorization": f"Bearer {self._creds.token}"}
        r = req.get(f"{_GMAIL_BASE}/{path}", headers=headers, params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _fetch_message(self, msg_id: str) -> dict:
        return self._get(f"messages/{msg_id}", {"format": "full"})

    def _search(self, query: str, max_results: int = _MAX_RESULTS) -> list[dict]:
        """Gmail 検索クエリでメッセージ一覧を返す"""
        data = self._get("messages", {"q": query, "maxResults": max_results})
        messages = data.get("messages", [])
        result = []
        for m in messages:
            try:
                detail = self._fetch_message(m["id"])
                result.append(detail)
            except Exception as e:
                logger.warning(f"メッセージ取得失敗 ({m['id']}): {e}")
        return result

    def _to_mail_info(self, msg: dict) -> dict:
        """Gmail メッセージ → 扱いやすい dict に変換"""
        headers = msg.get("payload", {}).get("headers", [])
        ts_ms = int(msg.get("internalDate", 0))
        received_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        body = _decode_body(msg.get("payload", {}))
        return {
            "id": msg["id"],
            "subject": _header(headers, "Subject"),
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "date": received_at.strftime("%Y/%m/%d %H:%M"),
            "received_at": received_at,
            "snippet": msg.get("snippet", ""),
            "body": body[:2000],  # Gemini に渡す本文は2000文字まで
        }

    # ------------------------------------------------------------------ #
    #  請求書メール
    # ------------------------------------------------------------------ #

    def get_invoice_emails(self, year: int = None, month: int = None) -> list[dict]:
        """
        今月（または指定月）の請求書メールを返す。
        判定は呼び出し元（Gemini）に委ねるため全候補を返す。
        known_senders が指定されている場合は OR 条件で絞り込む。
        """
        now = datetime.now(timezone(timedelta(hours=9)))
        y = year or now.year
        m = month or now.month
        after = f"{y}/{m:02d}/01"
        # 翌月1日
        if m == 12:
            before = f"{y + 1}/01/01"
        else:
            before = f"{y}/{m + 1:02d}/01"

        # 送信元ドメイン・アドレスを OR クエリに展開
        sender_q = ""
        if self.invoice_senders:
            from_parts = " OR ".join(f"from:{s}" for s in self.invoice_senders)
            sender_q = f"({from_parts}) OR "

        # 件名キーワードと送信元を組み合わせる
        keyword_q = "(subject:請求書 OR subject:invoice OR subject:Invoice OR subject:ご請求)"
        query = f"{sender_q}{keyword_q} after:{after} before:{before}"
        logger.info(f"Gmail 請求書検索: {query}")
        msgs = self._search(query)
        return [self._to_mail_info(m) for m in msgs]

    # ------------------------------------------------------------------ #
    #  未返信メール
    # ------------------------------------------------------------------ #

    def get_unreplied_candidates(self) -> list[dict]:
        """
        受信トレイにあり、自分宛に届き、N日以上経過したメールを返す。
        「返信が必要か」の判断は Gemini に委ねる。
        """
        # older_than: で N 日以上前を絞り込む
        # is:inbox で受信トレイのみ
        # -from:me で自分が送ったものを除外
        query = f"in:inbox -from:me older_than:{self.unreplied_days}d"
        logger.info(f"Gmail 未返信候補検索: {query}")
        msgs = self._search(query, max_results=30)
        return [self._to_mail_info(m) for m in msgs]
