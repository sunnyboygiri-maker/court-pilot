"""
Case polling.

poll_all_cases (beat) picks the cases that are due and enqueues one poll_case
task per CourtCase. Cases are shared, so a CNR tracked by 50 lawyers is still
polled once. poll_case fetches from eCourts under a global rate limit, diffs
against the stored record, snapshots changes, and triggers notifications.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from config.settings import settings
from config.timeutils import today_ist, utcnow
from models.database import CaseSnapshot, CaseStatus, CourtCase, TrackedCase
from models.session import worker_session
from scraper.ecourts import CaseDiffDetector, ECourtsScraper
from scraper.persist import apply_case_data, case_to_dict, comparable
from workers.celery_app import celery_app
from workers.redis_client import acquire_rate_slot, get_sync_redis

logger = logging.getLogger("courtpilot.workers.poll")

ERROR_BACKOFF_THRESHOLD = 3
MAX_BACKOFF = timedelta(hours=48)
DISPOSED_REPOLL = timedelta(days=7)
UPCOMING_WINDOW_DAYS = 7


@dataclass
class PollResult:
    case_id: int
    ok: bool
    changed: bool = False
    changes: dict = field(default_factory=dict)
    data_hash: Optional[str] = None
    error: Optional[str] = None


def _is_due(court_case: CourtCase, now) -> bool:
    last = court_case.last_polled_at
    if last is None:
        return True
    min_gap = timedelta(hours=settings.ECOURTS_MIN_REPOLL_HOURS)
    errors = court_case.poll_error_count or 0
    if errors >= ERROR_BACKOFF_THRESHOLD:
        # Exponential backoff after repeated failures: 1x, 2x, 4x... the poll interval
        interval = timedelta(hours=settings.ECOURTS_POLL_INTERVAL_HOURS)
        min_gap = max(min_gap, min(interval * 2 ** (errors - ERROR_BACKOFF_THRESHOLD), MAX_BACKOFF))
    if court_case.status == CaseStatus.DISPOSED:
        min_gap = max(min_gap, DISPOSED_REPOLL)
    return now - last >= min_gap


async def select_due_cases(db: AsyncSession) -> list[int]:
    """
    IDs of tracked cases due for polling: hearings in the next 7 days first,
    then least recently polled.
    """
    tracked_ids = select(TrackedCase.case_id).distinct()
    rows = (
        await db.scalars(
            select(CourtCase)
            .options(
                load_only(
                    CourtCase.id,
                    CourtCase.last_polled_at,
                    CourtCase.poll_error_count,
                    CourtCase.status,
                    CourtCase.next_hearing_date,
                )
            )
            .where(CourtCase.id.in_(tracked_ids))
        )
    ).all()

    now = utcnow()
    today = today_ist()
    soon = today + timedelta(days=UPCOMING_WINDOW_DAYS)
    due = [c for c in rows if _is_due(c, now)]

    def priority(c: CourtCase):
        upcoming = c.next_hearing_date is not None and today <= c.next_hearing_date <= soon
        return (
            0 if upcoming else 1,
            c.next_hearing_date if upcoming else today,
            c.last_polled_at is not None,
            c.last_polled_at or now,
        )

    return [c.id for c in sorted(due, key=priority)]


async def poll_one(db: AsyncSession, case_id: int, scraper: ECourtsScraper) -> Optional[PollResult]:
    court_case = await db.get(CourtCase, case_id)
    if court_case is None:
        return None

    try:
        data = await scraper.fetch_case_by_cnr(court_case.cnr_number)
    except Exception as e:
        court_case.poll_error_count = (court_case.poll_error_count or 0) + 1
        court_case.last_polled_at = utcnow()
        await db.commit()
        logger.warning("Poll failed for %s (%d consecutive): %s", court_case.cnr_number, court_case.poll_error_count, e)
        return PollResult(case_id, ok=False, error=str(e))

    result = PollResult(case_id, ok=True, data_hash=data.get("data_hash"))
    if data.get("data_hash") != court_case.data_hash:
        result.changes = CaseDiffDetector.detect_changes(case_to_dict(court_case), comparable(data))
        result.changed = True
        db.add(
            CaseSnapshot(
                case_id=court_case.id,
                data_hash=data["data_hash"],
                snapshot_data={k: v for k, v in data.items() if k != "raw_data"},
                changes=result.changes or None,
            )
        )

    # Always refresh the record (parties, advocates etc. aren't in the hash)
    apply_case_data(court_case, data)
    court_case.last_polled_at = utcnow()
    court_case.poll_error_count = 0
    await db.commit()
    return result


def _make_scraper() -> ECourtsScraper:
    return ECourtsScraper(
        max_concurrent=settings.ECOURTS_MAX_CONCURRENT,
        rate_limit_rpm=settings.ECOURTS_RATE_LIMIT_PER_MINUTE,
        use_mobile_fallback=settings.ECOURTS_MOBILE_API_FALLBACK,
    )


# --- Celery tasks ---

@celery_app.task(name="workers.tasks.poll_cases.poll_all_cases")
def poll_all_cases() -> int:
    async def run():
        async with worker_session() as db:
            return await select_due_cases(db)

    case_ids = asyncio.run(run())
    for case_id in case_ids:
        poll_case.delay(case_id)
    logger.info("Enqueued %d case polls", len(case_ids))
    return len(case_ids)


@celery_app.task(bind=True, name="workers.tasks.poll_cases.poll_case", max_retries=None)
def poll_case(self, case_id: int) -> dict:
    redis = get_sync_redis()
    wait = acquire_rate_slot(redis, "ecourts", settings.ECOURTS_RATE_LIMIT_PER_MINUTE)
    if wait:
        raise self.retry(countdown=wait)

    # Guard against the same case being polled twice at once (overlapping beats)
    lock = f"poll-lock:{case_id}"
    if not redis.set(lock, 1, nx=True, ex=15 * 60):
        return {"case_id": case_id, "skipped": "already polling"}

    async def run():
        scraper = _make_scraper()
        try:
            async with worker_session() as db:
                return await poll_one(db, case_id, scraper)
        finally:
            await scraper.close()

    try:
        result = asyncio.run(run())
    finally:
        redis.delete(lock)

    if result is None:
        return {"case_id": case_id, "skipped": "not found"}
    if result.changes and CaseDiffDetector.should_notify(result.changes):
        from workers.tasks.send_notifications import send_case_update

        send_case_update.delay(case_id, result.changes, result.data_hash)
    return {"case_id": case_id, "ok": result.ok, "changed": result.changed, "error": result.error}
