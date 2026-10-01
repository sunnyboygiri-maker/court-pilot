"""
Builds the per-channel content (Telegram text, email, WhatsApp template
params) for each notification type.
"""
from datetime import date
from typing import Optional

from bot import message_templates as tg
from models.database import CourtCase, TrackedCase
from notifications.dispatcher import NotificationContent
from notifications.email import render_email
from scraper.persist import case_title, ecourts_link


def _display_name(court_case: CourtCase, tracked: Optional[TrackedCase]) -> str:
    return tracked.label if tracked and tracked.label else case_title(court_case)


def _case_ctx(court_case: CourtCase, tracked: Optional[TrackedCase]) -> dict:
    return {
        "case": court_case,
        "tracked": tracked,
        "title": _display_name(court_case, tracked),
        "view_url": tg.view_url(court_case),
        "ecourts_url": ecourts_link(court_case),
        "fmt_date": tg.fmt_date,
    }


def hearing_reminder(court_case: CourtCase, days_until: int, tracked: Optional[TrackedCase] = None) -> NotificationContent:
    name = _display_name(court_case, tracked)
    when = "tomorrow" if days_until == 1 else f"in {days_until} days"
    hearing = tg.fmt_date(court_case.next_hearing_date)
    return NotificationContent(
        telegram_text=tg.format_hearing_reminder(court_case, days_until, tracked),
        email_subject=f"Hearing {when}: {name} ({hearing})",
        email_html=render_email("hearing_reminder.html", days_until=days_until, when=when, **_case_ctx(court_case, tracked)),
        email_text=f"Hearing {when}: {name}\n{hearing} at {court_case.court_name or 'court'}\n{tg.view_url(court_case)}",
        whatsapp_template="hearing_reminder",
        whatsapp_params=[name, hearing, court_case.court_name or "court", tg.view_url(court_case)],
    )


def weekly_digest(items: list[tuple[CourtCase, Optional[TrackedCase]]], week_start: date) -> NotificationContent:
    items = sorted(items, key=lambda it: it[0].next_hearing_date or date.max)
    rows = [
        {
            "date": tg.fmt_date(c.next_hearing_date),
            "title": _display_name(c, t),
            "court": c.court_name,
            "view_url": tg.view_url(c),
        }
        for c, t in items
    ]
    count = len(rows)
    summary = "; ".join(f"{r['date']}: {r['title']}" for r in rows[:5])
    if count > 5:
        summary += f"; +{count - 5} more"
    return NotificationContent(
        telegram_text=tg.format_weekly_digest(items, week_start),
        email_subject=f"Your hearings this week: {count} listed (week of {week_start:%d %b})",
        email_html=render_email("weekly_digest.html", rows=rows, count=count, week_start=week_start),
        email_text="\n".join(f"{r['date']} - {r['title']} - {r['view_url']}" for r in rows),
        whatsapp_template="weekly_digest",
        whatsapp_params=[str(count), summary],
    )


def new_order(court_case: CourtCase, order: dict, tracked: Optional[TrackedCase] = None) -> NotificationContent:
    name = _display_name(court_case, tracked)
    link = order.get("link") or tg.view_url(court_case)
    return NotificationContent(
        telegram_text=tg.format_new_order_alert(court_case, order, tracked),
        email_subject=f"New order uploaded: {name}",
        email_html=render_email("new_order.html", order=order, **_case_ctx(court_case, tracked)),
        email_text=f"New order in {name} dated {order.get('date') or '-'}.\n{link}",
        whatsapp_template="new_order",
        whatsapp_params=[name, link],
    )


def case_update(court_case: CourtCase, changes: dict, tracked: Optional[TrackedCase] = None) -> NotificationContent:
    name = _display_name(court_case, tracked)
    lines = [tg.describe_change(f, c) for f, c in changes.items() if f != "new_orders"]
    return NotificationContent(
        telegram_text=tg.format_case_update(court_case, changes, tracked),
        email_subject=f"Case update: {name}",
        email_html=render_email("case_update.html", changes=lines, **_case_ctx(court_case, tracked)),
        email_text=f"Update in {name}:\n" + "\n".join(lines) + f"\n{tg.view_url(court_case)}",
        whatsapp_template="case_update",
        whatsapp_params=[name, "; ".join(lines), tg.view_url(court_case)],
    )
