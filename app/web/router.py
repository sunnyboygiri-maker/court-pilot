"""
Lawyer-facing web app: server-rendered Jinja pages enhanced with htmx.

Every form also works without JavaScript (plain POST → redirect). With htmx,
<body hx-boost> turns navigation and forms into in-place swaps; a few forms
on the case page swap just their own card (hx-select) and show "Saved".
Business logic is shared with the API via app.cases.service.
"""
import hmac
import logging
import re
import secrets
from datetime import timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import otp
from app.auth.phone import normalize_phone
from app.auth.router import get_or_create_by_email, get_or_create_user, telegram_chat_for
from app.cases import service
from app.redis import get_redis
from app.views.router import build_timeline
from bot.message_templates import fmt_date
from app.web.session import (
    current_web_user,
    end_session,
    get_web_user,
    is_htmx,
    redirect,
    render,
    start_session,
    verify_csrf,
)
from config.settings import settings
from config.timeutils import today_ist
from models.database import (
    PLAN_LIMITS, WHATSAPP_ADDON_PRICE, CaseSnapshot, CaseStatus, CourtCase, PlanTier, TrackedCase, User,
    UserCourt,
)
from models.session import get_db
from notifications.sms import sms_configured
from scraper.ecourts import ECourtsScraper
from scraper.persist import case_title, ecourts_link, normalize_cnr, parse_date

logger = logging.getLogger("courtpilot.web")

router = APIRouter(include_in_schema=False)

PRIORITIES = {0: "Normal", 1: "High", 2: "Urgent"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PLAN_NAMES = {PlanTier.FREE: "Free", PlanTier.STARTER: "Starter", PlanTier.PRO: "Pro", PlanTier.FIRM: "Firm"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_email = TypeAdapter(EmailStr)


# --- Shared helpers ---

_bot_username: Optional[str] = None


async def bot_username() -> Optional[str]:
    """The bot's @username, from settings or asked once from Telegram."""
    global _bot_username
    if settings.TELEGRAM_BOT_USERNAME:
        return settings.TELEGRAM_BOT_USERNAME.lstrip("@")
    if _bot_username is None and settings.TELEGRAM_BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe")
                _bot_username = r.json()["result"]["username"]
        except Exception:
            logger.warning("Couldn't look up the Telegram bot username; set TELEGRAM_BOT_USERNAME")
    return _bot_username


async def page_context(db: AsyncSession, user: User, nav: str) -> dict:
    return {
        "user": user,
        "nav": nav,
        "cases_used": await service.count_tracked(db, user.id),
        "case_limit": service.case_limit(user),
        "plan_name": PLAN_NAMES[service.effective_plan(user)],
    }


def card(tracked: TrackedCase, today) -> dict:
    """Everything a case card or row shows."""
    c = tracked.court_case
    d = c.next_hearing_date
    days = (d - today).days if d else None
    if c.status == CaseStatus.DISPOSED:
        when = "Disposed"
    elif days is None:
        when = "Date not listed yet"
    elif days == 0:
        when = "Today"
    elif days == 1:
        when = "Tomorrow"
    elif days > 1:
        when = f"In {days} days"
    else:
        when = "Waiting for next date"
    return {
        "id": c.id,
        "tracked": tracked,
        "case": c,
        "title": case_title(c),
        "date": d,
        "days": days,
        "when": when,
        "disposed": c.status == CaseStatus.DISPOSED,
        "priority": PRIORITIES.get(tracked.priority or 0, "Normal"),
    }


def group_hearings(rows: list[TrackedCase], today) -> list[dict]:
    groups = {
        "today": {"key": "today", "title": "Today", "items": []},
        "tomorrow": {"key": "tomorrow", "title": "Tomorrow", "items": []},
        "week": {"key": "week", "title": "This week", "items": []},
        "later": {"key": "later", "title": "Later", "items": []},
        "stale": {"key": "stale", "title": "Waiting for next date", "items": [],
                  "note": "The last listed date has passed. We'll update these as soon as eCourts does."},
        "nodate": {"key": "nodate", "title": "No date listed", "items": []},
    }
    for t in rows:
        c = card(t, today)
        if c["disposed"]:
            continue
        days = c["days"]
        if days is None:
            key = "nodate"
        elif days < 0:
            key = "stale"
        elif days == 0:
            key = "today"
        elif days == 1:
            key = "tomorrow"
        elif days <= 7:
            key = "week"
        else:
            key = "later"
        groups[key]["items"].append(c)
    return list(groups.values())


# --- Login: phone, email or Google ---

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_STATE_COOKIE = "cp_oauth_state"


def google_configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def login_page_ctx(**ctx) -> dict:
    return {
        # Shown in DEBUG even when unconfigured, so the button can be previewed
        "google_enabled": google_configured() or settings.DEBUG,
        "email_enabled": otp.email_configured() or settings.DEBUG,
        **ctx,
    }


def login_render(request: Request, **ctx):
    return render(request, "web/login.html", login_page_ctx(**ctx))


def finish_login(request: Request, user: User, created: bool):
    if not user.is_active:
        return login_render(request, step="start", error="This account has been switched off. Please contact us.")
    response = redirect(request, "/", f"Welcome to CourtPilot, {user.name}!" if created else "You're logged in.")
    start_session(response, request, user.id)
    return response


def phone_channel() -> str:
    return "by SMS" if sms_configured() else "on Telegram"


@router.get("/login")
async def login_page(request: Request, method: str = "", db: AsyncSession = Depends(get_db)):
    if await current_web_user(request, db):
        return redirect(request, "/")
    return login_render(request, step="email" if method == "email" else "start")


# Phone

@router.post("/login/send", dependencies=[Depends(verify_csrf)])
async def login_send(
    request: Request,
    phone: str = Form(""),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    normalized = normalize_phone(phone)
    if normalized is None:
        return login_render(request, step="start", phone=phone,
                            error="Please enter your 10-digit mobile number, like 98765 43210.")

    existing = await db.scalar(select(User).where(User.phone == normalized))
    ctx = {"step": "code", "method": "phone", "target": normalized, "display_target": f"+91 {normalized[3:]}",
           "channel": phone_channel(), "is_new": existing is None, "resend_in": settings.OTP_RESEND_COOLDOWN_SECONDS}
    try:
        code = await otp.issue_otp(redis, normalized, await telegram_chat_for(db, normalized))
    except otp.OTPNoChannel:
        return login_render(request, step="telegram", phone=normalized, display_phone=normalized[3:],
                            bot_username=await bot_username())
    except otp.OTPCooldown as e:
        if "today" in str(e):
            return login_render(request, step="start", phone=phone, error=str(e) + ".")
        ctx["notice"] = f"We sent you a code {phone_channel()} less than a minute ago."
        return login_render(request, **ctx)
    except otp.OTPError as e:
        return login_render(request, step="start", phone=phone, error=str(e) + ".")
    if settings.DEBUG:
        ctx["debug_code"] = code
    return login_render(request, **ctx)


@router.post("/login/verify", dependencies=[Depends(verify_csrf)])
async def login_verify(
    request: Request,
    phone: str = Form(""),
    code: str = Form(""),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    normalized = normalize_phone(phone)
    if normalized is None:
        return redirect(request, "/login")
    existing = await db.scalar(select(User).where(User.phone == normalized))
    ctx = {"step": "code", "method": "phone", "target": normalized, "display_target": f"+91 {normalized[3:]}",
           "channel": phone_channel(), "is_new": existing is None, "name": name, "resend_in": 0}
    error = await _check_code(redis, normalized, code, name, existing is None)
    if error:
        return login_render(request, **ctx, error=error)
    user, created = await get_or_create_user(db, normalized, name)
    return finish_login(request, user, created)


async def _check_code(redis: Redis, key: str, code: str, name: str, is_new: bool) -> Optional[str]:
    """Validate the code form; returns an error message or None once the code is accepted."""
    code = re.sub(r"\D", "", code)
    if is_new and not name.strip():
        return "Please tell us your name."
    if len(code) != 6:
        return "The code has 6 digits. Please check and try again."
    if not await otp.verify_code(redis, key, code):
        return "That code is wrong or has expired. Please check it, or ask for a new code."
    return None


# Email

def _clean_email(value: str) -> Optional[str]:
    try:
        return _email.validate_python(value.strip()).lower()
    except ValidationError:
        return None


async def _email_user(db: AsyncSession, email: str) -> Optional[User]:
    return await db.scalar(select(User).where(func.lower(User.email) == email, User.email_verified.is_(True)))


@router.post("/login/email/send", dependencies=[Depends(verify_csrf)])
async def login_email_send(
    request: Request,
    email: str = Form(""),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    address = _clean_email(email)
    if address is None:
        return login_render(request, step="email", email=email, error="Please enter a valid email address.")
    ctx = {"step": "code", "method": "email", "target": address, "display_target": address,
           "channel": "by email", "is_new": await _email_user(db, address) is None,
           "resend_in": settings.OTP_RESEND_COOLDOWN_SECONDS}
    try:
        code = await otp.issue_email_code(redis, address)
    except otp.OTPCooldown as e:
        if "today" in str(e):
            return login_render(request, step="email", email=email, error=str(e) + ".")
        ctx["notice"] = "We emailed you a code less than a minute ago. Check your inbox and spam folder."
        return login_render(request, **ctx)
    except otp.OTPError as e:
        return login_render(request, step="email", email=email, error=str(e) + ".")
    if settings.DEBUG:
        ctx["debug_code"] = code
    return login_render(request, **ctx)


@router.post("/login/email/verify", dependencies=[Depends(verify_csrf)])
async def login_email_verify(
    request: Request,
    email: str = Form(""),
    code: str = Form(""),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    address = _clean_email(email)
    if address is None:
        return redirect(request, "/login?method=email")
    is_new = await _email_user(db, address) is None
    error = await _check_code(redis, f"email:{address}", code, name, is_new)
    if error:
        return login_render(request, step="code", method="email", target=address, display_target=address,
                            channel="by email", is_new=is_new, name=name, resend_in=0, error=error)
    user, created = await get_or_create_by_email(db, address, name)
    return finish_login(request, user, created)


# Google

def _google_redirect_uri() -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/login/google/callback"


@router.get("/login/google")
async def login_google(request: Request):
    if not google_configured():
        return login_render(request, step="start",
                            error="Google sign-in isn't set up yet. Please use your phone number or email.")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=303)
    # Lax so it comes back on Google's top-level redirect to the callback
    response.set_cookie(OAUTH_STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
    return response


@router.get("/login/google/callback")
async def login_google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    failed = "We couldn't sign you in with Google. Please try again, or use your phone number or email."
    expected = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    if error == "access_denied":
        return _google_done(login_render(request, step="start", error="Google sign-in was cancelled."))
    if not code or not expected or not hmac.compare_digest(state, expected) or not google_configured():
        return _google_done(login_render(request, step="start", error=failed))
    try:
        profile = await google_profile(code)
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("Google sign-in failed")
        return _google_done(login_render(request, step="start", error=failed))
    address = (profile.get("email") or "").lower()
    if not address or profile.get("email_verified") is not True:
        return _google_done(login_render(request, step="start", error=failed))
    user, created = await get_or_create_by_email(db, address, profile.get("name"))
    return _google_done(finish_login(request, user, created))


async def google_profile(code: str) -> dict:
    """Swap the authorisation code for the user's Google profile (email, email_verified, name)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        })
        token.raise_for_status()
        info = await client.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"})
        info.raise_for_status()
        return info.json()


def _google_done(response):
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    response = redirect(request, "/login", "You've been logged out.")
    end_session(response)
    return response


# --- Home ---

@router.get("/")
async def dashboard(request: Request, user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    today = today_ist()
    rows = await service.list_tracked(db, user.id, limit=500)
    groups = group_hearings(rows, today)
    ctx = await page_context(db, user, "home")
    ctx.update(
        groups=groups,
        has_cases=bool(rows),
        all_disposed=bool(rows) and not any(g["items"] for g in groups),
        counts={g["key"]: len(g["items"]) for g in groups},
        today=today,
        telegram_linked=bool(user.telegram_chat_id),
        show_fab=True,
    )
    return render(request, "web/dashboard.html", ctx)


# --- All cases ---

@router.get("/cases")
async def cases_list(
    request: Request,
    q: str = "",
    status: str = "",
    priority: str = "",
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
):
    status_filter = {"pending": CaseStatus.PENDING, "disposed": CaseStatus.DISPOSED}.get(status)
    priority_filter = int(priority) if priority in ("0", "1", "2") else None
    rows = await service.list_tracked(
        db, user.id, status=status_filter, priority=priority_filter, search=q.strip()[:100] or None, limit=500
    )
    today = today_ist()
    ctx = await page_context(db, user, "cases")
    ctx.update(
        items=[card(t, today) for t in rows],
        q=q, status=status, priority=priority,
        filtered=bool(q or status or priority),
        show_fab=True,
    )
    return render(request, "web/cases.html", ctx)


# --- Add case ---

# GET /cases/new (the "Add a case" hub with its search tabs) lives in app/web/find.py


@router.post("/cases/new", dependencies=[Depends(verify_csrf)])
async def add_case(
    request: Request,
    cnr: str = Form(""),
    label: str = Form(""),
    client_name: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    scraper: ECourtsScraper = Depends(service.get_scraper),
):
    form = {"cnr": cnr, "label": label, "client_name": client_name, "notes": notes}
    error, error_link = None, None
    try:
        tracked = await service.track_case(
            db, user, cnr, scraper,
            label=label.strip()[:255] or None,
            client_name=client_name.strip()[:255] or None,
            notes=notes.strip() or None,
        )
    except service.InvalidCNR:
        error = ("That doesn't look like a CNR number. It has 16 letters and numbers, "
                 "like DLHC010582482024. You'll find it at the top of the case page on eCourts.")
    except service.AlreadyTracked:
        error = "You're already tracking this case."
        existing = await db.scalar(select(CourtCase.id).where(CourtCase.cnr_number == normalize_cnr(cnr)))
        if existing:
            error_link = (f"/cases/{existing}/view", "Open the case")
    except service.PlanLimitReached:
        limit = service.case_limit(user)
        error = (f"You're tracking {limit} of {limit} cases allowed on the "
                 f"{PLAN_NAMES[service.effective_plan(user)]} plan. Stop tracking an old case to add this one.")
        error_link = ("/settings#plan", "See plans")
    except service.CaseNotFound:
        error = ("eCourts has no case with this CNR number. Please check each character and try again. "
                 "Newly filed cases can take a few days to appear.")
    except service.UpstreamError:
        error = ("eCourts isn't responding right now. This often happens during court hours. "
                 "Please try again in a few minutes.")
    if error:
        from app.web.find import hub_context

        ctx = await hub_context(db, user, "cnr", error=error, error_link=error_link)
        ctx["form"].update(form)
        return render(request, "web/case_new.html", ctx)
    return redirect(request, f"/cases/{tracked.case_id}/view", "Case added. We'll remind you before every hearing.")


# --- Case detail ---

async def _case_page(request: Request, db: AsyncSession, user: User, case_id: int, **extra):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
    except service.CaseNotFound:
        ctx = await page_context(db, user, "cases")
        return render(request, "web/not_found.html", ctx, status_code=404)
    c = tracked.court_case
    today = today_ist()
    snapshots = (
        await db.scalars(select(CaseSnapshot).where(CaseSnapshot.case_id == c.id).order_by(CaseSnapshot.captured_at))
    ).all()
    orders = sorted(c.orders_json or [], key=lambda o: str(o.get("date") or ""), reverse=True)
    for o in orders:
        d = parse_date(o.get("date"))
        o["label"] = fmt_date(d) if d else (o.get("date") or "Date not given")
        o["safe_link"] = o.get("link") if str(o.get("link") or "").startswith(("https://", "http://")) else None
    ctx = await page_context(db, user, "cases")
    ctx.update(
        c=card(tracked, today),
        case=c,
        tracked=tracked,
        orders=orders,
        timeline=build_timeline(c, list(snapshots)),
        ecourts_url=ecourts_link(c),
        priorities=PRIORITIES,
        whatsapp_unlocked=service.has_whatsapp(user),
        telegram_linked=bool(user.telegram_chat_id),
        has_email=bool(user.email),
    )
    ctx.update(extra)
    return render(request, "web/case_detail.html", ctx)


@router.get("/cases/{case_id}/view")
async def case_detail(request: Request, case_id: int, user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    return await _case_page(request, db, user, case_id)


async def _saved(request: Request, db: AsyncSession, user: User, case_id: int, section: str, message: str):
    if is_htmx(request):
        # Only this card gets swapped in (hx-select), so show the confirmation inside it
        return await _case_page(request, db, user, case_id, saved=section)
    return redirect(request, f"/cases/{case_id}/view", message)


@router.post("/cases/{case_id}/edit", dependencies=[Depends(verify_csrf)])
async def case_edit(
    request: Request,
    case_id: int,
    label: str = Form(""),
    client_name: str = Form(""),
    notes: str = Form(""),
    priority: int = Form(0),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
    except service.CaseNotFound:
        return redirect(request, "/cases", "That case isn't in your list any more.")
    tracked.label = label.strip()[:255] or None
    tracked.client_name = client_name.strip()[:255] or None
    tracked.notes = notes.strip() or None
    tracked.priority = priority if priority in PRIORITIES else 0
    await db.commit()
    return await _saved(request, db, user, case_id, "edit", "Saved.")


@router.post("/cases/{case_id}/alerts", dependencies=[Depends(verify_csrf)])
async def case_alerts(
    request: Request,
    case_id: int,
    notify_telegram: Optional[str] = Form(None),
    notify_email: Optional[str] = Form(None),
    notify_whatsapp: Optional[str] = Form(None),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
    except service.CaseNotFound:
        return redirect(request, "/cases", "That case isn't in your list any more.")
    tracked.notify_telegram = notify_telegram is not None
    tracked.notify_email = notify_email is not None
    tracked.notify_whatsapp = notify_whatsapp is not None and service.has_whatsapp(user)
    await db.commit()
    return await _saved(request, db, user, case_id, "alerts", "Reminder settings saved.")


@router.post("/cases/{case_id}/untrack", dependencies=[Depends(verify_csrf)])
async def case_untrack(request: Request, case_id: int, user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
        title = tracked.label or case_title(tracked.court_case)
        await service.untrack_case(db, user.id, case_id)
    except service.CaseNotFound:
        return redirect(request, "/cases")
    return redirect(request, "/cases", f"Stopped tracking {title}.")


# --- Settings ---

TELEGRAM_LINK_TTL = 30 * 60


async def telegram_link(redis: Redis, user: User) -> Optional[str]:
    """
    One-tap "Connect Telegram" link for this account: t.me/<bot>?start=link_<token>.
    The bot attaches the chat to whoever owns the token, so it works for accounts
    without a phone number (Google/email sign-ups) too.
    """
    username = await bot_username()
    if not username:
        return None
    token = secrets.token_urlsafe(18)
    await redis.set(f"tg-link:{token}", user.id, ex=TELEGRAM_LINK_TTL)
    return f"https://t.me/{username}?start=link_{token}"


async def _settings_page(request: Request, db: AsyncSession, redis: Redis, user: User, **extra):
    ctx = await page_context(db, user, "settings")
    current = service.effective_plan(user)
    ctx.update(
        bot_username=await bot_username(),
        telegram_link=None if user.telegram_chat_id else await telegram_link(redis, user),
        my_courts=list((await db.scalars(select(UserCourt).where(UserCourt.user_id == user.id).order_by(UserCourt.id))).all()),
        weekdays=WEEKDAYS,
        plans=[
            {"tier": t, "name": PLAN_NAMES[t], "max_cases": v["max_cases"], "price": v["price_monthly"], "current": t == current}
            for t, v in PLAN_LIMITS.items()
        ],
        whatsapp_price=WHATSAPP_ADDON_PRICE,
        whatsapp_active=service.has_whatsapp(user),
        plan_expires=user.plan_expires_at if current != PlanTier.FREE else None,
        profile=extra.pop("profile", None) or {
            "name": user.name, "email": user.email or "", "bar": user.bar_registration_no or "",
        },
        reminders=extra.pop("reminders", None) or {
            "time": user.notification_time or "08:00",
            "day": user.digest_day if user.digest_day is not None else 0,
        },
    )
    ctx.update(extra)
    return render(request, "web/settings.html", ctx)


@router.get("/settings")
async def settings_page(
    request: Request,
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    return await _settings_page(request, db, redis, user)


def _email_change_keys(user: User) -> tuple[str, str]:
    return f"email-change:{user.id}", f"email-change-to:{user.id}"


@router.post("/settings/profile", dependencies=[Depends(verify_csrf)])
async def settings_profile(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    bar: str = Form(""),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    profile = {"name": name, "email": email, "bar": bar}
    name, bar = name.strip(), bar.strip()
    new_email = None
    error = None
    if not name:
        error = "Please enter your name."
    elif email.strip():
        new_email = _clean_email(email)
        if new_email is None:
            error = "That email address doesn't look right."
    if error:
        return await _settings_page(request, db, redis, user, profile=profile, profile_error=error)

    user.name = name[:255]
    user.bar_registration_no = bar[:50] or None
    pending = None
    if new_email is None:
        user.email, user.email_verified = None, False
    elif new_email != (user.email or "") or not user.email_verified:
        taken = await _email_user(db, new_email)
        if taken is not None and taken.id != user.id:
            return await _settings_page(request, db, redis, user, profile=profile,
                                        profile_error="That email is already used by another CourtPilot account.")
        # Saved only once they type the code we email them
        code_key, addr_key = _email_change_keys(user)
        try:
            code = await otp.issue_email_code(redis, new_email, key=code_key)
        except otp.OTPCooldown:
            code = None  # a code went out moments ago; let them type it
        except otp.OTPError as e:
            await db.commit()
            return await _settings_page(request, db, redis, user, profile=profile, profile_error=str(e) + ".")
        await redis.set(addr_key, new_email, ex=settings.OTP_TTL_SECONDS)
        pending = {"email": new_email, "debug_code": code if settings.DEBUG else None}
    await db.commit()
    if pending:
        return await _settings_page(request, db, redis, user, email_pending=pending)
    if is_htmx(request):
        return await _settings_page(request, db, redis, user, saved="profile")
    return redirect(request, "/settings", "Profile saved.")


@router.post("/settings/email/verify", dependencies=[Depends(verify_csrf)])
async def settings_email_verify(
    request: Request,
    code: str = Form(""),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    code_key, addr_key = _email_change_keys(user)
    new_email = await redis.get(addr_key)
    if not new_email:
        return await _settings_page(request, db, redis, user,
                                    profile_error="That code has expired. Please save your email again to get a new one.")
    if not await otp.verify_code(redis, code_key, re.sub(r"\D", "", code)):
        return await _settings_page(request, db, redis, user, email_pending={"email": new_email},
                                    email_code_error="That code is wrong or has expired.")
    holder = await db.scalar(select(User).where(func.lower(User.email) == new_email, User.id != user.id))
    if holder is not None:
        if holder.email_verified:
            return await _settings_page(request, db, redis, user,
                                        profile_error="That email is already used by another CourtPilot account.")
        holder.email = None  # unproven claim on an address this user just proved they own
        await db.flush()
    user.email, user.email_verified = new_email, True
    await db.commit()
    await redis.delete(addr_key)
    if is_htmx(request):
        return await _settings_page(request, db, redis, user, saved="profile")
    return redirect(request, "/settings", "Email confirmed.")


@router.post("/settings/reminders", dependencies=[Depends(verify_csrf)])
async def settings_reminders(
    request: Request,
    time: str = Form("08:00"),
    day: int = Form(0),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    time = time.strip()[:5]
    if not TIME_RE.match(time) or day not in range(7):
        return await _settings_page(request, db, redis, user, reminders={"time": time, "day": day},
                                    reminders_error="Please choose a time and a day.")
    user.notification_time = time
    user.digest_day = day
    await db.commit()
    if is_htmx(request):
        return await _settings_page(request, db, redis, user, saved="reminders")
    return redirect(request, "/settings", "Reminder settings saved.")
