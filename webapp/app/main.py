"""
Wasabi 管理 WebApp（FastAPI）

チームプロファイル（wasabi_teams）の閲覧・編集を行う管理画面。
SmartSync WebApp と同じ構成（Cloud Run + IAP / ローカルは DEBUG_MODE=true）。

ローカル起動:
  set DEBUG_MODE=true
  uvicorn app.main:app --reload --port 8000   （webapp/ ディレクトリから）
"""
import os
import sys
import logging

# リポジトリルート（src/ を import するため）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

# ローカル（社内プロキシ）向け SSL 設定。Cloud Run では不要だが無害
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from fastapi import FastAPI, Request, Response

from app.routers import kb, pages, qa, teams

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Wasabi Admin")

_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:;"
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    return response


app.include_router(pages.router, tags=["pages"])
app.include_router(teams.router, prefix="/api", tags=["api"])
app.include_router(kb.router, prefix="/api", tags=["kb"])
app.include_router(qa.router, prefix="/api", tags=["qa"])


@app.get("/healthz")
async def healthz():
    return {"ok": True}
