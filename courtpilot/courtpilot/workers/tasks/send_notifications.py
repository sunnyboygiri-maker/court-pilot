"""
Scheduled and on-demand notifications.

Every send is claimed in Redis first (claim_once), so beat re-runs, retries
and overlapping workers never deliver the same reminder twice.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from redis import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config.timeutils import now_ist
from models.database import CaseStatus, CourtCase, NotificationChannel, NotificationType, TrackedCase, User
from models.session import worker_session
from notifications import content
from notifications.dispatcher import dispatch_notification, enabled_channels
from scraper.ecourts import CaseDiffDetector
from workers.celery_app import celery_app
from workers.redis_client import claim_once, get_sync_redis

logger = logging.getLogger("courtpilot.workers.notify")

SLOT_MINUTES = 15
DEDUP_TTL = 8 * 24 * 3600
REMINDER_TYPES = {3: NotificationType.THREE_DAY, 2: NotificationType.TWO_DAY, 1: NotificationType.ONE_DAY}


def current_slot(now: datetime) -> tuple[str, str]:
    """The [start, end) HH:MM window of the 15-minute beat tick `now` falls in."""
    start = now.replace(minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0)
    end = start + timedelta(minutes=SLOT_MINUTES)
    end_str = "24:00" if end.date() != start.date() else end.strftime("%H:%M")
    return start.strftime("%H:%M"), end_str


def _in_slot(slot: Optional[tuple[str, str]]):
    if slot is None:
        return True
    start, end = slot
    return (User.notification_time >= start) & (User.notification_time < end)


async def send_reminders(
    db: AsyncSession, redis: Redis, now: datetime, days_ahead: list[int], slot: Optional[tuple[str, str]]
) -> int:
    """
    Remind every user tracking a case whose hearing is `days_ahead` days from
    today. With a slot, only users whose notification_time falls in it.
    """
    today = now.date()
    by_date = {today + timedelta(days=d): d for d in days_ahead}
    rows = (
        await db.scalars(
            select(TrackedCase)
            .join(TrackedCase.court_case)
            .join(TrackedCase.user)
            .options(selectinload(TrackedCase.court_case), selectinload(TrackedCase.user))
            .where(
                CourtCase.next_hearing_date.in_(list(by_date)),
                CourtCase.status != CaseStatus.DISPOSED,
                User.is_active.is_(True),
                _in_slot(slot),
            )
        )
    ).all()

    sent = 0
    for tracked in rows:
        court_case, user = tracked.court_case, tracked.user
        days = by_date[court_case.next_hearing_date]
        ntype = REMINDER_TYPES[days]
        if not enabled_channels(user, tracked):
            continue
        key = f"notif:{ntype.value}:{user.id}:{court_case.id}:{court_case.next_hearing_date}"
        if not claim_once(redis, key, DEDUP_TTL):
            continue
        await dispatch_notification(
            db, user, court_case, ntype, content.hearing_reminder(court_case, days, tracked), tracked=tracked
        )
        sent += 1
    await db.commit()
    return sent


async def send_digests(db: AsyncSession, redis: Redis, now: datetime, slot: Optional[tuple[str, str]]) -> int:
    """Weekly digest for users whose digest_day is today and notification_time is in the slot."""
    today = now.date()
    week_end = today + timedelta(days=6)
    users = (
        await db.scalars(
            select(User).where(User.is_active.is_(True), User.digest_day == today.weekday(), _in_slot(slot))
        )
    ).all()

    sent = 0
    for user in users:
        tracked_rows = (
            await db.scalars(
                select(TrackedCase)
                .join(TrackedCase.court_case)
                .options(selectinload(TrackedCase.court_case))
                .where(
                    TrackedCase.user_id == user.id,
                    CourtCase.next_hearing_date >= today,
                    CourtCase.next_hearing_date <= week_end,
                    CourtCase.status != CaseStatus.DISPOSED,
                )
            )
        ).all()
        if not tracked_rows:
            continue
        # A channel is used if the user has it on for at least one case in the digest
        channels = [
            ch for ch in enabled_channels(user)
            if any(ch in enabled_channels(user, t) for t in tracked_rows)
        ]
        if not channels:
            continue
        if not claim_once(redis, f"notif:digest:{user.id}:{today}", DEDUP_TTL):
            continue
        items = [(t.court_case, t) for t in tracked_rows]
        await dispatch_notification(
            db, user, None, NotificationType.WEEKLY_DIGEST, content.weekly_digest(items, today), channels=channels
        )
        sent += 1
    await db.commit()
    return sent


async def send_update(db: AsyncSession, redis: Redis, case_id: int, changes: dict, data_hash: str) -> int:
    """Alert everyone tracking a case that polling found changed."""
    types = CaseDiffDetector.should_notify(changes)
    if not types:
        return 0
    court_case = await db.get(CourtCase, case_id)
    if court_case is None:
        return 0
    rows = (
        await db.scalars(
            select(TrackedCase)
            .join(TrackedCase.user)
            .options(selectinload(TrackedCase.user))
            .where(TrackedCase.case_id == case_id, User.is_active.is_(True))
        )
    ).all()

    new_orders = (changes.get("new_orders") or {}).get("orders") or [] if "new_order" in types else []
    has_update = any(t != "new_order" for t in types)

    sent = 0
    for tracked in rows:
        user = tracked.user
        if not enabled_channels(user, tracked):
            continue
        for i, order in enumerate(new_orders):
            if claim_once(redis, f"notif:order:{user.id}:{case_id}:{data_hash}:{i}", DEDUP_TTL):
                await dispatch_notification(
                    db, user, court_case, NotificationType.NEW_ORDER,
                    content.new_order(court_case, order, tracked), tracked=tracked,
                )
                sent += 1
        if has_update and claim_once(redis, f"notif:update:{user.id}:{case_id}:{data_hash}", DEDUP_TTL):
            await dispatch_notification(
                db, user, court_case, NotificationType.CASE_UPDATE,
                content.case_update(court_case, changes, tracked), tracked=tracked,
            )
            sent += 1
    await db.commit()
    return sent


# --- Celery tasks ---

def _run(coro_factory):
    async def run():
        async with worker_session() as db:
            return await coro_factory(db, get_sync_redis())

    return asyncio.run(run())


@celery_app.task(name="workers.tasks.send_notifications.send_hearing_reminders")
def send_hearing_reminders() -> int:
    """3- and 2-day reminders, at each user's notification_time."""
    now = now_ist()
    return _run(lambda db, r: send_reminders(db, r, now, [3, 2], current_slot(now)))


@celery_app.task(name="workers.tasks.send_notifications.send_evening_reminders")
def send_evening_reminders() -> int:
    """1-day ("hearing tomorrow") reminder, sent to everyone at REMINDER_EVENING_TIME."""
    now = now_ist()
    return _run(lambda db, r: send_reminders(db, r, now, [1], None))


@celery_app.task(name="workers.tasks.send_notifications.send_weekly_digest")
def send_weekly_digest() -> int:
    now = now_ist()
    return _run(lambda db, r: send_digests(db, r, now, current_slot(now)))


@celery_app.task(name="workers.tasks.send_notifications.send_case_update")
def send_case_update(case_id: int, changes: dict, data_hash: str) -> int:
    return _run(lambda db, r: send_update(db, r, case_id, changes, data_hash))
