"""
Case tracking business logic, shared by the REST API and the Telegram bot.
"""
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config.settings import settings
from config.timeutils import today_ist, utcnow
from models.database import PLAN_LIMITS, CaseStatus, CourtCase, PlanTier, TrackedCase, User
from scraper.ecourts import CaseNotFoundError, ECourtsScraper
from scraper.persist import apply_case_data, case_title, ecourts_link, normalize_cnr

logger = logging.getLogger("courtpilot.cases")


class CaseServiceError(Exception):
    status_code = 400


class InvalidCNR(CaseServiceError):
    status_code = 422


class PlanLimitReached(CaseServiceError):
    status_code = 402


class AlreadyTracked(CaseServiceError):
    status_code = 409


class CaseNotFound(CaseServiceError):
    status_code = 404


class UpstreamError(CaseServiceError):
    status_code = 502


_scraper: Optional[ECourtsScraper] = None


def get_scraper() -> ECourtsScraper:
    global _scraper
    if _scraper is None:
        _scraper = ECourtsScraper(
            max_concurrent=settings.ECOURTS_MAX_CONCURRENT,
            rate_limit_rpm=settings.ECOURTS_RATE_LIMIT_PER_MINUTE,
            use_mobile_fallback=settings.ECOURTS_MOBILE_API_FALLBACK,
        )
    return _scraper


# --- Plans ---

def effective_plan(user: User) -> PlanTier:
    """Paid plans lapse back to FREE once plan_expires_at has passed."""
    if user.plan != PlanTier.FREE and user.plan_expires_at and user.plan_expires_at < utcnow():
        return PlanTier.FREE
    return user.plan


def case_limit(user: User) -> int:
    return PLAN_LIMITS[effective_plan(user)]["max_cases"]


def has_whatsapp(user: User) -> bool:
    """The add-on is billed with the plan, so it lapses with it."""
    if not user.whatsapp_addon:
        return False
    return not (user.plan_expires_at and user.plan_expires_at < utcnow())


async def count_tracked(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(TrackedCase).where(TrackedCase.user_id == user_id))


# --- Tracking ---

async def get_or_fetch_case(db: AsyncSession, cnr: str, scraper: ECourtsScraper) -> CourtCase:
    court_case = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == cnr))
    if court_case is not None:
        return court_case

    try:
        data = await scraper.fetch_case_by_cnr(cnr)
    except CaseNotFoundError as e:
        raise CaseNotFound(str(e))
    except Exception as e:
        logger.exception("eCourts fetch failed for %s", cnr)
        raise UpstreamError(f"Could not fetch case from eCourts right now: {e}")

    court_case = CourtCase(cnr_number=cnr)
    apply_case_data(court_case, data)
    court_case.court_ref = data.get("court_ref")
    court_case.last_polled_at = utcnow()
    db.add(court_case)
    try:
        await db.commit()
    except IntegrityError:
        # Another user added the same CNR while we were scraping
        await db.rollback()
        return await db.scalar(select(CourtCase).where(CourtCase.cnr_number == cnr))
    from scraper.extras import needs_enrich, request_enrich

    if needs_enrich(court_case, None, set()):
        request_enrich(court_case.id)  # judge's name and the latest order's text, in the background
    return court_case


async def track_case(
    db: AsyncSession,
    user: User,
    cnr_input: str,
    scraper: ECourtsScraper,
    **metadata,
) -> TrackedCase:
    cnr = normalize_cnr(cnr_input)
    if cnr is None:
        raise InvalidCNR("CNR must be 16 characters, e.g. DLHC010582482024")

    # Check limits before scraping; scraping is the expensive part
    existing = await db.scalar(
        select(TrackedCase)
        .join(CourtCase)
        .where(TrackedCase.user_id == user.id, CourtCase.cnr_number == cnr)
    )
    if existing is not None:
        raise AlreadyTracked("You are already tracking this case")
    limit = case_limit(user)
    if await count_tracked(db, user.id) >= limit:
        raise PlanLimitReached(
            f"Your {effective_plan(user).value} plan allows {limit} cases. Upgrade to track more."
        )

    court_case = await get_or_fetch_case(db, cnr, scraper)
    tracked = TrackedCase(
        user_id=user.id,
        case_id=court_case.id,
        notify_whatsapp=has_whatsapp(user),
        **{k: v for k, v in metadata.items() if v is not None},
    )
    db.add(tracked)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AlreadyTracked("You are already tracking this case")
    return await get_tracked(db, user.id, court_case.id)


async def get_tracked(db: AsyncSession, user_id: int, case_id: int) -> TrackedCase:
    tracked = await db.scalar(
        select(TrackedCase)
        .options(selectinload(TrackedCase.court_case))
        .where(TrackedCase.user_id == user_id, TrackedCase.case_id == case_id)
    )
    if tracked is None:
        raise CaseNotFound("Case not found in your tracked cases")
    return tracked


async def get_tracked_by_cnr(db: AsyncSession, user_id: int, cnr: str) -> TrackedCase:
    tracked = await db.scalar(
        select(TrackedCase)
        .join(CourtCase)
        .options(selectinload(TrackedCase.court_case))
        .where(TrackedCase.user_id == user_id, CourtCase.cnr_number == cnr)
    )
    if tracked is None:
        raise CaseNotFound("Case not found in your tracked cases")
    return tracked


async def untrack_case(db: AsyncSession, user_id: int, case_id: int) -> None:
    tracked = await get_tracked(db, user_id, case_id)
    await db.delete(tracked)
    await db.commit()


async def list_tracked(
    db: AsyncSession,
    user_id: int,
    *,
    status: Optional[CaseStatus] = None,
    hearing_from=None,
    hearing_to=None,
    search: Optional[str] = None,
    priority: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TrackedCase]:
    q = (
        select(TrackedCase)
        .join(CourtCase)
        .options(selectinload(TrackedCase.court_case))
        .where(TrackedCase.user_id == user_id)
    )
    if status is not None:
        q = q.where(CourtCase.status == status)
    if hearing_from is not None:
        q = q.where(CourtCase.next_hearing_date >= hearing_from)
    if hearing_to is not None:
        q = q.where(CourtCase.next_hearing_date <= hearing_to)
    if priority is not None:
        q = q.where(TrackedCase.priority == priority)
    if search:
        like = f"%{search}%"
        q = q.where(
            or_(
                CourtCase.cnr_number.ilike(like),
                CourtCase.petitioner.ilike(like),
                CourtCase.respondent.ilike(like),
                CourtCase.case_number.ilike(like),
                TrackedCase.label.ilike(like),
                TrackedCase.client_name.ilike(like),
            )
        )
    q = q.order_by(
        CourtCase.next_hearing_date.is_(None),
        CourtCase.next_hearing_date,
        TrackedCase.priority.desc(),
    ).limit(limit).offset(offset)
    return list((await db.scalars(q)).all())


async def upcoming(db: AsyncSession, user_id: int, days: int = 7) -> list[TrackedCase]:
    today = today_ist()
    return await list_tracked(db, user_id, hearing_from=today, hearing_to=today + timedelta(days=days))


# --- Serialization helpers ---

def view_url(court_case: CourtCase) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/case/{court_case.cnr_number}/view"


def court_case_payload(court_case: CourtCase, detail: bool = False) -> dict:
    data = {c.name: getattr(court_case, c.name) for c in CourtCase.__table__.columns}
    data["title"] = case_title(court_case)
    data["ecourts_url"] = ecourts_link(court_case)
    data["view_url"] = view_url(court_case)
    if detail:
        data["orders"] = court_case.orders_json or []
    return data


def tracked_payload(tracked: TrackedCase, detail: bool = False) -> dict:
    return {
        "tracking_id": tracked.id,
        "label": tracked.label,
        "notes": tracked.notes,
        "client_name": tracked.client_name,
        "is_petitioner_side": tracked.is_petitioner_side,
        "priority": tracked.priority or 0,
        "notify_telegram": bool(tracked.notify_telegram),
        "notify_email": bool(tracked.notify_email),
        "notify_whatsapp": bool(tracked.notify_whatsapp),
        "tracked_since": tracked.created_at,
        "case": court_case_payload(tracked.court_case, detail=detail),
    }
