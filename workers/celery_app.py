"""
Celery app and beat schedule. All crontabs are in IST.

  celery -A workers.celery_app worker -l info
  celery -A workers.celery_app beat -l info
"""
from celery import Celery
from celery.schedules import crontab

from config.settings import settings

celery_app = Celery(
    "courtpilot",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["workers.tasks.poll_cases", "workers.tasks.send_notifications", "workers.tasks.search"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    # eCourts lookups take tens of seconds each; on their own queue (served by
    # the `poller` service) a big poll batch can't delay reminders, which stay
    # on the default "celery" queue
    task_routes={
        # Background reading of whole cause lists: keep it off the searcher lawyers wait on
        "workers.tasks.search.refresh_cause_lists": {"queue": "polling"},
        "workers.tasks.poll_cases.*": {"queue": "polling"},
        # Lawyers wait on screen for these, so they get their own worker
        "workers.tasks.search.*": {"queue": "search"},
    },
)


def _hour_minute(hhmm: str) -> tuple[int, int]:
    hour, minute = hhmm.split(":")
    return int(hour), int(minute)


_poll_hours = max(1, min(settings.ECOURTS_POLL_INTERVAL_HOURS, 23))
_evening_hour, _evening_minute = _hour_minute(settings.REMINDER_EVENING_TIME)

celery_app.conf.beat_schedule = {
    # Enqueues one poll_case task per due case; poll_case enforces the eCourts rate limit
    "poll-all-cases": {
        "task": "workers.tasks.poll_cases.poll_all_cases",
        "schedule": crontab(minute=0, hour=f"*/{_poll_hours}"),
    },
    # 3- and 2-day reminders and the weekly digest go out at each user's own
    # notification_time (default 08:00), so check every 15-minute slot
    "morning-reminders": {
        "task": "workers.tasks.send_notifications.send_hearing_reminders",
        "schedule": crontab(minute="*/15"),
    },
    "weekly-digest": {
        "task": "workers.tasks.send_notifications.send_weekly_digest",
        "schedule": crontab(minute="*/15"),
    },
    # Cause lists for today and tomorrow: courts publish them the evening before
    # (sometimes late), so look in the evening, at night and first thing in the morning
    "cause-lists": {
        "task": "workers.tasks.search.refresh_cause_lists",
        "schedule": crontab(hour="7,18,21", minute=10),
    },
    # 1-day reminder is an evening message for everyone
    "evening-reminders": {
        "task": "workers.tasks.send_notifications.send_evening_reminders",
        "schedule": crontab(hour=_evening_hour, minute=_evening_minute),
    },
}

# `celery -A workers.celery_app` looks for an attribute named `app` or `celery`
app = celery_app
