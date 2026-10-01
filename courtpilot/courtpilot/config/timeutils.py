"""All user-facing times are IST; DB timestamps are naive UTC."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def utcnow() -> datetime:
    """Naive UTC, matching the DateTime columns in models.database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
