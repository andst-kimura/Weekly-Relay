"""
HTML ページ（ダッシュボード / チーム詳細）
"""
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.deps import CurrentUser, get_current_user

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: CurrentUser = Depends(get_current_user)):
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"user": user},
    )


@router.get("/teams/{team_id}", response_class=HTMLResponse)
async def team_detail(request: Request, team_id: str,
                       user: CurrentUser = Depends(get_current_user)):
    return templates.TemplateResponse(
        request=request, name="team.html",
        context={"user": user, "team_id": team_id},
    )
