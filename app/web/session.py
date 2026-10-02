"""
Web app plumbing: cookie sessions, CSRF, flash messages and page rendering.

- Session: a WEB-type JWT in an httpOnly cookie (Secure on HTTPS, SameSite=Lax).
  API bearer tokens and web cookies aren't interchangeable.
- CSRF: double-submit. A random token lives in an httpOnly cookie and is echoed
  in every form as `csrf_token`; POSTs must carry a matching value.
- Flash: a one-shot message cookie shown as a toast on the next page.
"""
import hmac
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import WEB, TokenError, create_web_token, decode_token
from bot.message_templates import fmt_date
from config.settings import settings
from models.database import User
from models.session import get_db

SESSION_COOKIE = "cp_session"
CSRF_COOKIE = "cp_csrf"
FLASH_COOKIE = "cp_flash"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["fmt_date"] = fmt_date
templates.env.globals["app_name"] = settings.APP_NAME
templates.env.filters["phone"] = lambda p: f"{p[:3]} {p[3:8]} {p[8:]}" if p and p.startswith("+91") and len(p) == 13 else p


class WebAuthRedirect(Exception):
    """Raised by get_web_user; turned into a redirect to /login."""


async def web_auth_redirect_handler(request: Request, exc: WebAuthRedirect) -> Response:
    if request.headers.get("HX-Request"):
        # htmx would swap the login page into the current one; ask it to navigate instead
        response = Response(status_code=200, headers={"HX-Redirect": "/login"})
    else:
        response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _cookie_kwargs(request: Request) -> dict:
    return {"httponly": True, "secure": _secure(request), "samesite": "lax", "path": "/"}


# --- Session ---

async def current_web_user(request: Request, db: AsyncSession) -> Optional[User]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        user_id = decode_token(token, WEB)
    except TokenError:
        return None
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def get_web_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await current_web_user(request, db)
    if user is None:
        raise WebAuthRedirect()
    return user


def start_session(response: Response, request: Request, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, create_web_token(user_id), max_age=settings.WEB_SESSION_DAYS * 86400, **_cookie_kwargs(request)
    )


def end_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# --- CSRF ---

class CSRFFailed(Exception):
    pass


async def verify_csrf(request: Request) -> None:
    """Dependency for every state-changing web route."""
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = request.cookies.get(CSRF_COOKIE) or ""
    if not expected or not hmac.compare_digest(str(sent), expected):
        raise CSRFFailed()


async def csrf_failed_handler(request: Request, exc: CSRFFailed) -> Response:
    """Send them back to the page they came from (which issues a fresh token) with a note."""
    back = "/"
    referer = urlsplit(request.headers.get("referer") or "")
    if referer.netloc == request.url.netloc and referer.path.startswith("/"):
        back = referer.path
    response = redirect(request, back, "This page had expired. Please try again.")
    response.delete_cookie(CSRF_COOKIE, path="/")
    if is_htmx(request):
        response = Response(status_code=200, headers={"HX-Redirect": back})
        response.set_cookie(FLASH_COOKIE, quote("This page had expired. Please try again."), max_age=60, **_cookie_kwargs(request))
        response.delete_cookie(CSRF_COOKIE, path="/")
    return response


# --- Rendering ---

def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template: str, context: Optional[dict] = None, status_code: int = 200) -> Response:
    csrf = request.cookies.get(CSRF_COOKIE)
    new_csrf = csrf is None
    if new_csrf:
        csrf = secrets.token_urlsafe(32)
    flash = request.cookies.get(FLASH_COOKIE)
    ctx = {"csrf_token": csrf, "flash": unquote(flash) if flash else None, "debug": settings.DEBUG}
    ctx.update(context or {})
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if new_csrf:
        response.set_cookie(CSRF_COOKIE, csrf, **_cookie_kwargs(request))
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(request: Request, url: str, flash: Optional[str] = None) -> Response:
    response = RedirectResponse(url, status_code=303)
    if flash:
        response.set_cookie(FLASH_COOKIE, quote(flash), max_age=60, **_cookie_kwargs(request))
    return response
