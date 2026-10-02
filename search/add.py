"""
Track cases straight from search results.

Search results already carry the CNR, parties and case number, so the case is
added at once (no 6-second eCourts lookup per case, which matters when a
lawyer imports 40 cases) and the full details are fetched in the background.
That first fetch is quiet: poll_one doesn't alert on a case it has never
fully seen.
"""
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import service
from config.settings import settings
from models.database import CaseStatus, CourtCase, CourtType, SearchHit, TrackedCase, User

logger = logging.getLogger("courtpilot.search.add")


@dataclass
class AddResult:
    added: list[int] = field(default_factory=list)       # CourtCase ids now tracked
    already: list[int] = field(default_factory=list)     # were tracked before
    over_limit: int = 0                                   # skipped: plan full
    new_case_ids: list[int] = field(default_factory=list)  # need a first full fetch


def _number_only(case_number: str | None) -> str | None:
    """"SPCS/33/2018" -> "33/2018" (CourtCase keeps the type separately)."""
    if not case_number or "/" not in case_number:
        return case_number
    return case_number.split("/", 1)[1]


async def add_hits(db: AsyncSession, user: User, hits: list[SearchHit]) -> AddResult:
    result = AddResult()
    limit = service.case_limit(user)
    used = await service.count_tracked(db, user.id)
    for hit in hits:
        court_case = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == hit.cnr_number))
        if court_case is not None:
            tracked = await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id,
                                                                TrackedCase.case_id == court_case.id))
            if tracked is not None:
                result.already.append(court_case.id)
                continue
        if used >= limit:
            result.over_limit += 1
            continue
        if court_case is None:
            court_case = CourtCase(
                cnr_number=hit.cnr_number,
                case_type=hit.case_type,
                case_number=_number_only(hit.case_number),
                petitioner=hit.petitioner,
                respondent=hit.respondent,
                court_name=hit.court_name,
                court_type=CourtType.DISTRICT,
                status=CaseStatus.UNKNOWN,
            )
            db.add(court_case)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                court_case = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == hit.cnr_number))
            else:
                result.new_case_ids.append(court_case.id)
        db.add(TrackedCase(user_id=user.id, case_id=court_case.id, notify_whatsapp=service.has_whatsapp(user)))
        await db.flush()
        used += 1
        result.added.append(court_case.id)
    await db.commit()
    return result


def fetch_details(case_ids: list[int]) -> None:
    """Get the full eCourts record for newly added cases, promptly."""
    if not case_ids:
        return
    if settings.SEARCH_INLINE:
        asyncio.get_running_loop().create_task(_fetch_inline(case_ids))
        return
    from workers.tasks.search import fetch_new_case

    for case_id in case_ids:
        fetch_new_case.delay(case_id)


async def _fetch_inline(case_ids: list[int]) -> None:
    from models.session import SessionLocal
    from workers.tasks.poll_cases import poll_one

    scraper = service.get_scraper()
    for case_id in case_ids:
        try:
            async with SessionLocal() as db:
                await poll_one(db, case_id, scraper)
        except Exception:
            logger.exception("First fetch failed for case %s", case_id)
