from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.cases import service
from config.timeutils import IST, today_ist, utcnow
from models.database import (
    CaseSnapshot,
    CaseStatus,
    CourtCase,
    NotificationChannel,
    NotificationLog,
    NotificationType,
    PlanTier,
    TrackedCase,
)
from notifications import dispatcher
from notifications.base import DeliveryResult
from tests.conftest import CNR_A, CNR_B, make_detail
from workers.tasks import poll_cases, send_notifications
from workers.tasks.send_notifications import current_slot


@pytest.fixture
def outbox(monkeypatch):
    """Capture deliveries instead of calling Telegram/SMTP/Meta."""
    sent = []

    async def telegram(chat_id, text, parse_mode="MarkdownV2"):
        sent.append(("telegram", chat_id, text))
        return DeliveryResult(True)

    async def email(to, subject, html, text=None):
        sent.append(("email", to, subject))
        return DeliveryResult(True)

    async def whatsapp(phone, template, params):
        sent.append(("whatsapp", phone, template, params))
        return DeliveryResult(True, cost_inr=0.8)

    monkeypatch.setattr(dispatcher, "send_telegram_message", telegram)
    monkeypatch.setattr(dispatcher, "send_email", email)
    monkeypatch.setattr(dispatcher, "send_whatsapp_message", whatsapp)
    return sent


async def _track(db, user, cnr, scraper):
    return await service.track_case(db, user, cnr, scraper)


def ist(days=0, hour=8, minute=0):
    d = today_ist() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


# --- Polling ---

async def test_select_due_cases_prioritises_and_dedups(db, make_user, scraper):
    u1 = await make_user()
    u2 = await make_user(phone="+919999999999")
    for u in (u1, u2):
        await _track(db, u, CNR_A, scraper)  # hearing in 5 days
    await _track(db, u1, CNR_B, scraper)  # hearing in 20 days

    a = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_A))
    b = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_B))
    # Both freshly polled: nothing due
    assert await poll_cases.select_due_cases(db) == []

    a.last_polled_at = b.last_polled_at = utcnow() - timedelta(hours=5)
    b.last_polled_at = utcnow() - timedelta(hours=50)  # older, but hearing is further out
    untracked = CourtCase(cnr_number="KAMY010000012020")  # nobody tracks it
    db.add(untracked)
    await db.commit()
    assert await poll_cases.select_due_cases(db) == [a.id, b.id]  # each CNR once, upcoming first


async def test_error_backoff(db, user, scraper):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    case.poll_error_count = 4  # backoff = 2x the 6h interval
    case.last_polled_at = utcnow() - timedelta(hours=7)
    await db.commit()
    assert await poll_cases.select_due_cases(db) == []
    case.last_polled_at = utcnow() - timedelta(hours=13)
    await db.commit()
    assert await poll_cases.select_due_cases(db) == [case.id]


async def test_poll_unchanged_case(db, user, scraper):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    result = await poll_cases.poll_one(db, case.id, scraper)
    assert result.ok and not result.changed and result.changes == {}
    assert (await db.scalars(select(CaseSnapshot))).all() == []


async def test_poll_detects_changes_and_snapshots(db, user, scraper):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    new_date = today_ist() + timedelta(days=12)
    scraper.details[CNR_A] = make_detail(CNR_A, next_hearing=new_date, orders=2, stage="Arguments")

    result = await poll_cases.poll_one(db, case.id, scraper)
    assert result.changed
    assert result.changes["next_hearing_date"]["new"] == new_date.isoformat()
    assert result.changes["stage"] == {"old": "Evidence", "new": "Arguments"}
    assert result.changes["new_orders"]["count"] == 1
    assert "status" not in result.changes  # "Pending" vs stored enum must not look like a change

    await db.refresh(case)
    assert case.next_hearing_date == new_date and case.stage == "Arguments" and len(case.orders_json) == 2
    snap = await db.scalar(select(CaseSnapshot))
    assert snap.changes["stage"]["new"] == "Arguments" and "raw_data" not in snap.snapshot_data


async def test_poll_failure_counts_errors(db, user, scraper):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    scraper.fail.add(CNR_A)
    result = await poll_cases.poll_one(db, case.id, scraper)
    assert not result.ok
    await db.refresh(case)
    assert case.poll_error_count == 1
    scraper.fail.clear()
    await poll_cases.poll_one(db, case.id, scraper)
    await db.refresh(case)
    assert case.poll_error_count == 0


async def test_disposal_is_status_change(db, user, scraper):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    scraper.details[CNR_A] = make_detail(CNR_A, next_hearing=None, decided=today_ist())
    result = await poll_cases.poll_one(db, case.id, scraper)
    assert result.changes["status"] == {"old": "pending", "new": "disposed"}
    await db.refresh(case)
    assert case.status == CaseStatus.DISPOSED


# --- Case update notifications ---

async def test_case_update_goes_to_every_tracker_once(db, make_user, scraper, sync_redis, outbox):
    u1 = await make_user(telegram_chat_id="111", email="one@example.com")
    u2 = await make_user(phone="+919999999999", telegram_chat_id="222")
    for u in (u1, u2):
        await _track(db, u, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    scraper.details[CNR_A] = make_detail(CNR_A, next_hearing=today_ist() + timedelta(days=9), orders=2)
    result = await poll_cases.poll_one(db, case.id, scraper)

    sent = await send_notifications.send_update(db, sync_redis, case.id, result.changes, result.data_hash)
    assert sent == 4  # (new order + date change) x 2 users
    kinds = sorted((ch, dest) for ch, dest, *_ in outbox)
    assert kinds == [("email", "one@example.com"), ("email", "one@example.com"), ("telegram", "111"), ("telegram", "111"), ("telegram", "222"), ("telegram", "222")]

    logs = (await db.scalars(select(NotificationLog))).all()
    assert {l.notification_type for l in logs} == {NotificationType.NEW_ORDER, NotificationType.CASE_UPDATE}
    assert all(l.delivered for l in logs)

    # Re-running (e.g. a Celery retry) sends nothing new
    outbox.clear()
    assert await send_notifications.send_update(db, sync_redis, case.id, result.changes, result.data_hash) == 0
    assert outbox == []


async def test_stage_only_change_is_silent(db, user, sync_redis, outbox):
    changes = {"stage": {"old": "Evidence", "new": "Arguments"}}
    assert await send_notifications.send_update(db, sync_redis, 1, changes, "h") == 0


# --- Channel routing ---

async def test_per_case_channel_prefs_and_whatsapp_gating(db, make_user, scraper, sync_redis, outbox):
    from config.timeutils import utcnow

    u = await make_user(telegram_chat_id="111", email="me@example.com", whatsapp_number="+919876500000",
                        plan=PlanTier.PRO, whatsapp_addon=True, plan_expires_at=utcnow() + timedelta(days=10))
    tracked = await _track(db, u, CNR_A, scraper)
    assert tracked.notify_whatsapp is True
    tracked.notify_email = False
    await db.commit()

    case = await db.scalar(select(CourtCase))
    case.next_hearing_date = today_ist() + timedelta(days=3)
    await db.commit()
    await send_notifications.send_reminders(db, sync_redis, ist(), [3, 2], current_slot(ist()))
    assert sorted(o[0] for o in outbox) == ["telegram", "whatsapp"]
    wa = next(o for o in outbox if o[0] == "whatsapp")
    assert wa[1] == "+919876500000" and wa[2] == "hearing_reminder"
    log = await db.scalar(select(NotificationLog).where(NotificationLog.channel == NotificationChannel.WHATSAPP))
    assert log.cost_inr == 0.8

    # Add-on lapses with the plan
    u.plan_expires_at = utcnow() - timedelta(days=1)
    await db.commit()
    assert NotificationChannel.WHATSAPP not in dispatcher.enabled_channels(u, tracked)


# --- Scheduled reminders ---

async def test_reminders_by_days_and_slot(db, make_user, scraper, sync_redis, outbox):
    early = await make_user(telegram_chat_id="111")  # default 08:00
    late = await make_user(phone="+919999999999", telegram_chat_id="222", notification_time="09:10")
    await _track(db, early, CNR_A, scraper)
    await _track(db, late, CNR_A, scraper)
    await _track(db, early, CNR_B, scraper)

    a = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_A))
    b = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_B))
    a.next_hearing_date = today_ist() + timedelta(days=3)
    b.next_hearing_date = today_ist() + timedelta(days=4)  # not a reminder day
    await db.commit()

    now = ist(hour=8, minute=0)
    assert await send_notifications.send_reminders(db, sync_redis, now, [3, 2], current_slot(now)) == 1
    assert outbox[0][1] == "111" and "Hearing in 3 days" in outbox[0][2]

    now = ist(hour=9, minute=0)
    assert await send_notifications.send_reminders(db, sync_redis, now, [3, 2], current_slot(now)) == 1
    assert outbox[1][1] == "222"

    # Same slot again: deduped
    assert await send_notifications.send_reminders(db, sync_redis, now, [3, 2], current_slot(now)) == 0

    logs = (await db.scalars(select(NotificationLog))).all()
    assert {l.notification_type for l in logs} == {NotificationType.THREE_DAY}


async def test_evening_one_day_reminder_skips_disposed(db, make_user, scraper, sync_redis, outbox):
    u = await make_user(telegram_chat_id="111")
    await _track(db, u, CNR_A, scraper)
    await _track(db, u, CNR_B, scraper)
    a = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_A))
    b = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_B))
    a.next_hearing_date = b.next_hearing_date = today_ist() + timedelta(days=1)
    b.status = CaseStatus.DISPOSED
    await db.commit()

    assert await send_notifications.send_reminders(db, sync_redis, ist(hour=19), [1], None) == 1
    assert "Hearing tomorrow" in outbox[0][2]


async def test_users_without_channels_are_skipped(db, user, scraper, sync_redis, outbox):
    await _track(db, user, CNR_A, scraper)
    case = await db.scalar(select(CourtCase))
    case.next_hearing_date = today_ist() + timedelta(days=2)
    await db.commit()
    assert await send_notifications.send_reminders(db, sync_redis, ist(), [3, 2], current_slot(ist())) == 0
    assert (await db.scalars(select(NotificationLog))).all() == []


async def test_weekly_digest(db, make_user, scraper, sync_redis, outbox):
    today = today_ist()
    u = await make_user(telegram_chat_id="111", email="me@example.com", digest_day=today.weekday())
    other_day = await make_user(phone="+919999999999", telegram_chat_id="222", digest_day=(today.weekday() + 1) % 7)
    for cnr in (CNR_A, CNR_B):
        await _track(db, u, cnr, scraper)
    await _track(db, other_day, CNR_A, scraper)
    # Email is off for the only case in this week's digest (CNR_B is 20 days out)
    t_email_off = await db.scalar(
        select(TrackedCase).join(CourtCase).where(TrackedCase.user_id == u.id, CourtCase.cnr_number == CNR_A)
    )
    t_email_off.notify_email = False
    await db.commit()

    now = ist(hour=8, minute=3)
    assert await send_notifications.send_digests(db, sync_redis, now, current_slot(now)) == 1
    assert [o[0] for o in outbox] == ["telegram"]
    tg = outbox[0][2]
    assert "Weekly digest" in tg and "1 hearing coming up" in tg
    assert await send_notifications.send_digests(db, sync_redis, now, current_slot(now)) == 0


def test_current_slot():
    assert current_slot(ist(hour=8, minute=7)) == ("08:00", "08:15")
    assert current_slot(ist(hour=23, minute=50)) == ("23:45", "24:00")
