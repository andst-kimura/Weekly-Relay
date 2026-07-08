"""
認証・Firestore 接続の共通依存（SmartSync webapp のパターンを踏襲）

認証の優先順:
  1. IAP JWT（IAP_AUDIENCE 設定時・署名検証）
  2. X-Goog-Authenticated-User-Email（Cloud Run + IAP 背後のみ信用）
  3. DEBUG_MODE=true 時の X-Debug-User（ローカル開発用）

管理者判定: wasabi_admins/{email} の存在チェック
Firestore: src/smartsync_client の REST 実装を再利用（ローカル・Cloud Run 共通）
"""
import os
import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request

from src import smartsync_client as sc

logger = logging.getLogger(__name__)


@dataclass
class CurrentUser:
    email: str
    is_admin: bool


def _extract_email(request: Request) -> str:
    # 1. IAP JWT（署名検証）
    iap_audience = os.environ.get("IAP_AUDIENCE", "")
    if iap_audience:
        jwt_token = request.headers.get("X-Goog-IAP-JWT-Assertion", "")
        if jwt_token:
            from google.oauth2 import id_token
            import google.auth.transport.requests as google_requests
            try:
                payload = id_token.verify_token(
                    jwt_token, google_requests.Request(), audience=iap_audience)
                return payload.get("email", "")
            except Exception:
                raise HTTPException(status_code=401, detail="IAP JWT verification failed")

    # 2. IAP 無署名ヘッダー
    email = (
        request.headers.get("X-Goog-Authenticated-User-Email", "")
        .removeprefix("accounts.google.com:")
    )
    if email:
        return email

    # 3. ローカル開発（ヘッダー優先、なければ DEBUG_USER 環境変数 = ブラウザ確認用）
    if os.environ.get("DEBUG_MODE") == "true":
        return request.headers.get("X-Debug-User", "") or os.environ.get("DEBUG_USER", "")

    return ""


def is_admin_email(email: str) -> bool:
    if not email:
        return False
    try:
        url = sc._doc_url("wasabi_admins", email)
        resp = sc._get_session().get(url, timeout=15)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"wasabi_admins 照合失敗: {e}")
        return False


async def get_current_user(request: Request) -> CurrentUser:
    email = _extract_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return CurrentUser(email=email, is_admin=is_admin_email(email))


async def require_admin(request: Request) -> CurrentUser:
    user = await get_current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user
