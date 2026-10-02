"""
Telegram message templates (MarkdownV2).

Every piece of dynamic text goes through esc(); URLs go through link().
"""
from collections import defaultdict
from datetime import date
from typing import Iterable, Optional

from telegram.helpers import escape_markdown

from config.settings import settings
from models.database import CaseStatus, CourtCase, TrackedCase
from scraper.persist import case_title, ecourts_link

PRIORITY_BADGE = {1: "🔶 High", 2: "🔴 Urgent"}

FIELD_LABELS = {
    "next_hearing_date": "Next hearing",
    "status": "Status",
    "stage": "Stage",
    "judge": "Judge",
    "bench": "Bench",
}


def esc(value) -> str:
    return escape_markdown(str(value), version=2) if value not in (None, "") else "—"


def link(text: str, url: str) -> str:
    safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{esc(text)}]({safe_url})"


def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%a, %d %b %Y") if d else "Not listed"


def view_url(court_case: CourtCase) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/case/{court_case.cnr_number}/view"


def _links(court_case: CourtCase) -> str:
    return f"{link('Full details', view_url(court_case))}  •  {link('eCourts', ecourts_link(court_case))}"


def _heading(court_case: CourtCase, tracked: Optional[TrackedCase]) -> str:
    title = esc(case_title(court_case))
    if tracked and tracked.label:
        title = f"{esc(tracked.label)} \\({title}\\)"
    return f"*{title}*"


def format_case_summary(court_case: CourtCase, tracked: Optional[TrackedCase] = None) -> str:
    lines = [_heading(court_case, tracked)]
    if tracked and tracked.priority in PRIORITY_BADGE:
        lines.append(esc(PRIORITY_BADGE[tracked.priority]))
    lines.append(f"CNR: `{esc(court_case.cnr_number)}`")
    if court_case.case_type or court_case.case_number:
        lines.append(f"Case: {esc(' '.join(filter(None, [court_case.case_type, court_case.case_number])))}")
    lines.append(f"Court: {esc(court_case.court_name)}")
    if court_case.judge:
        lines.append(f"Judge: {esc(court_case.judge)}")
    status = court_case.status.value.title() if court_case.status else "Unknown"
    lines.append(f"Status: {esc(status)}" + (f" • Stage: {esc(court_case.stage)}" if court_case.stage else ""))
    lines.append(f"📅 Next hearing: *{esc(fmt_date(court_case.next_hearing_date))}*")
    if court_case.latest_order_date:
        order = esc(fmt_date(court_case.latest_order_date))
        if court_case.latest_order_link:
            order = f"{order} \\({link('PDF', court_case.latest_order_link)}\\)"
        lines.append(f"Latest order: {order}")
    if tracked and tracked.client_name:
        lines.append(f"Client: {esc(tracked.client_name)}")
    lines.append("")
    lines.append(_links(court_case))
    return "\n".join(lines)


def format_hearing_reminder(court_case: CourtCase, days_until: int, tracked: Optional[TrackedCase] = None) -> str:
    when = "tomorrow" if days_until == 1 else f"in {days_until} days"
    lines = [
        f"⏰ *Hearing {esc(when)}*",
        "",
        _heading(court_case, tracked),
        f"📅 {esc(fmt_date(court_case.next_hearing_date))}",
        f"🏛 {esc(court_case.court_name)}",
    ]
    if court_case.judge:
        lines.append(f"👤 {esc(court_case.judge)}")
    if court_case.stage:
        lines.append(f"Stage: {esc(court_case.stage)}")
    lines.append(f"CNR: `{esc(court_case.cnr_number)}`")
    lines.append("")
    lines.append(_links(court_case))
    return "\n".join(lines)


def format_weekly_digest(items: Iterable[tuple[CourtCase, Optional[TrackedCase]]], week_start: date) -> str:
    by_date: dict[date, list] = defaultdict(list)
    for court_case, tracked in items:
        by_date[court_case.next_hearing_date].append((court_case, tracked))
    total = sum(len(v) for v in by_date.values())

    lines = [
        f"📋 *Weekly digest — week of {esc(week_start.strftime('%d %b'))}*",
        esc(f"{total} hearing{'s' if total != 1 else ''} coming up"),
    ]
    for day in sorted(by_date):
        lines.append("")
        lines.append(f"*{esc(fmt_date(day))}*")
        for court_case, tracked in by_date[day]:
            name = tracked.label if tracked and tracked.label else case_title(court_case)
            court = f" — {court_case.court_name}" if court_case.court_name else ""
            lines.append(f"• {link(name, view_url(court_case))}{esc(court)}")
    return "\n".join(lines)


def format_new_order_alert(court_case: CourtCase, order: dict, tracked: Optional[TrackedCase] = None) -> str:
    lines = [
        "📄 *New order uploaded*",
        "",
        _heading(court_case, tracked),
        f"Order date: {esc(order.get('date'))}",
    ]
    if order.get("description"):
        lines.append(f"Type: {esc(order['description'])}")
    if order.get("link"):
        lines.append(link("Download order", order["link"]))
    lines.append(f"CNR: `{esc(court_case.cnr_number)}`")
    lines.append("")
    lines.append(_links(court_case))
    return "\n".join(lines)


def describe_change(field: str, change: dict) -> str:
    """Plain-text line for a change, e.g. 'Next hearing: 12 Oct → 19 Oct'."""
    old, new = change.get("old"), change.get("new")
    if field == "next_hearing_date":
        old, new = _pretty_date(old), _pretty_date(new)
    elif field == "status":
        old = old.title() if old else old
        new = new.title() if new else new
    label = FIELD_LABELS.get(field, field.replace("_", " ").title())
    return f"{label}: {old or '—'} → {new or '—'}"


def format_case_update(court_case: CourtCase, changes: dict, tracked: Optional[TrackedCase] = None) -> str:
    lines = ["🔔 *Case update*", "", _heading(court_case, tracked)]
    for field, change in changes.items():
        if field == "new_orders":
            continue
        lines.append(esc(describe_change(field, change)))
    if court_case.status == CaseStatus.DISPOSED:
        lines.append(esc("This case is now disposed."))
    lines.append(f"CNR: `{esc(court_case.cnr_number)}`")
    lines.append("")
    lines.append(_links(court_case))
    return "\n".join(lines)


def _pretty_date(value) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)
