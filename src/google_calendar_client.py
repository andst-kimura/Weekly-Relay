"""
Google Calendar クライアント
指定期間のスケジュールを取得し、工数を推定する
"""
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from datetime import datetime, timezone, timedelta
import requests as req
import os
import pickle
import logging

JST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]
TOKEN_FILE = "config/google_token.pickle"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    def __init__(self, credentials_file: str, calendar_ids: list[str]):
        self.credentials_file = credentials_file
        self.calendar_ids = calendar_ids
        self.creds = self._authenticate()

    def _authenticate(self):
        """Google OAuth2認証"""
        creds = None

        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "wb") as token:
                pickle.dump(creds, token)

        return creds

    def _get(self, url: str, params: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {self.creds.token}"}
        response = req.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_events(self, since: datetime, until: datetime) -> list[dict]:
        """全カレンダーからイベントを取得"""
        all_events = []

        for cal_id in self.calendar_ids:
            try:
                url = f"{CALENDAR_API_BASE}/calendars/{cal_id}/events"
                params = {
                    "timeMin": since.isoformat() + "Z",
                    "timeMax": until.isoformat() + "Z",
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 500,
                    "supportsAttachments": "true",
                }
                result = self._get(url, params)

                for event in result.get("items", []):
                    start = event.get("start", {})
                    end = event.get("end", {})

                    if "dateTime" in start:
                        start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(JST).replace(tzinfo=None)
                        end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone(JST).replace(tzinfo=None)
                        duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
                        is_all_day = False
                    else:
                        start_dt = datetime.fromisoformat(start["date"])
                        end_dt = datetime.fromisoformat(end["date"])
                        duration_minutes = 480
                        is_all_day = True

                    attendees = event.get("attendees", [])
                    my_status = "accepted"
                    for att in attendees:
                        if att.get("self"):
                            my_status = att.get("responseStatus", "accepted")
                            break
                    if my_status == "declined":
                        continue

                    # Gemini 議事録の添付ファイルを抽出（Google Docs 形式のみ）
                    attachments = [
                        a for a in event.get("attachments", [])
                        if a.get("mimeType") == "application/vnd.google-apps.document"
                    ]

                    all_events.append({
                        "calendar_id": cal_id,
                        "event_id": event["id"],
                        "summary": event.get("summary", "（タイトルなし）"),
                        "description": event.get("description", ""),
                        "start_dt": start_dt,
                        "end_dt": end_dt,
                        "duration_minutes": duration_minutes,
                        "duration_hours": round(duration_minutes / 60, 1),
                        "is_all_day": is_all_day,
                        "location": event.get("location", ""),
                        "attendees_count": len(attendees),
                        "my_status": my_status,
                        "attachments": attachments,
                    })

            except Exception as e:
                logger.warning(f"カレンダー '{cal_id}' の取得エラー: {e}")

        all_events.sort(key=lambda x: x["start_dt"])
        return all_events

    def summarize_by_day(self, events: list[dict]) -> dict:
        """日別の工数サマリーを作成"""
        by_day = {}
        for event in events:
            day = event["start_dt"].strftime("%Y-%m-%d (%a)")
            if day not in by_day:
                by_day[day] = {"events": [], "total_hours": 0}
            by_day[day]["events"].append(event)
            by_day[day]["total_hours"] += event["duration_hours"]
        return by_day