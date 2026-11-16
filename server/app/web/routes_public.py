"""Public HTML pages (no login): the dashboard at / and the charts at /trends.

The pages are static shells: numbers come from /api/public/* in the browser (dashboard.js / trends.js). The server
only decides the first paint: the demo banner and the empty state, so neither flashes in after load. No session
cookie is issued here: public readers have no forms, so nothing to protect with a CSRF token.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import DailyStat, Station
from app.web.routes_public_api import VISIBLE, _demo
from app.web.templating import env

router = APIRouter(include_in_schema=False)


def _page(request: Request, template: str, **ctx) -> HTMLResponse:
    html = env.get_template(template).render(request=request, csrf_token="", flash=None, admin=None, session_enabled=False, **ctx)
    return HTMLResponse(html)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    has_data = bool(session.scalar(select(func.count(Station.id)).where(*VISIBLE)))
    return _page(request, "dashboard.html", demo=_demo(session), has_data=has_data)


@router.get("/trends", response_class=HTMLResponse)
def trends(request: Request, session: Session = Depends(get_session)):
    has_data = session.scalar(
        select(DailyStat.id).join(Station, DailyStat.station_id == Station.id).where(*VISIBLE).limit(1)) is not None
    return _page(request, "trends.html", demo=_demo(session), has_data=has_data)
