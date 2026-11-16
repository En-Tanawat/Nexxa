from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.web import deps, security

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True, undefined=StrictUndefined)


def render(request: Request, template: str, *, status_code: int = 200, session_update: dict | None = None,
           **ctx) -> HTMLResponse:
    """Render with csrf_token, admin and the pending flash; issues an anonymous session so forms always have a token.

    session_update sets (or, with None, removes) extra keys in the signed session cookie. Never put a secret in it."""
    data = deps.session_data(request)
    new = dict(data or {})
    flash = new.pop("flash", None)
    if security.session_enabled() and not isinstance(new.get("csrf"), str):
        new = {"csrf": security.new_csrf_token()}
    for k, v in (session_update or {}).items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    ctx.setdefault("admin", getattr(request.state, "admin", None))
    html = env.get_template(template).render(
        request=request, csrf_token=new.get("csrf", ""), flash=flash,
        session_enabled=security.session_enabled(), **ctx,
    )
    response = HTMLResponse(html, status_code=status_code)
    if security.session_enabled() and new != data:
        deps.write_session(response, request, new)
    return response
