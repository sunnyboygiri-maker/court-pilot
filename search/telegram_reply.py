"""
Answer a screenshot sent on Telegram or WhatsApp.

Cases the search was sure about were already added (search.jobs.auto_add);
the reply says so, and offers Add buttons only for the ones it wasn't sure
about. summary() is channel-neutral; telegram_text()/whatsapp_text() render it.
"""
from dataclasses import dataclass, field

from bot.message_templates import esc
from config.settings import settings
from models.database import SearchHit, SearchJob
from notifications.telegram import send_telegram_message

MAX_LISTED = 8


@dataclass
class Summary:
    added: list[SearchHit] = field(default_factory=list)
    to_check: list[SearchHit] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # "CS DJ ADJ 912/2021 (court not visible on the screenshot)"
    over_limit: int = 0
    error: str = ""


def summary(job: SearchJob, hits: list[SearchHit]) -> Summary:
    added = set(job.params.get("added") or [])
    s = Summary(added=[h for h in hits if h.cnr_number in added], to_check=[h for h in hits if h.cnr_number not in added],
                over_limit=int(job.params.get("over_limit") or 0))
    if job.status == "failed" and not hits:
        s.error = job.error or "We couldn't read that screenshot."
    for r in job.params.get("read") or []:
        if not r.get("found"):
            label = f"{r.get('case_type', '')} {r.get('number', '')}/{r.get('year', '')}".strip(" /") or "One case"
            why = "court not visible on the screenshot" if not r.get("court_header") else "not found on eCourts"
            s.missing.append(f"{label} ({why})")
    return s


def _case_lines(h: SearchHit) -> list[str]:
    """Number, parties, court, then what tells similar cases apart: next date, stage, judge."""
    d = h.details or {}
    lines = [h.case_number or h.cnr_number]
    parties = f"{h.petitioner or ''}" + (f" vs {h.respondent}" if h.respondent else "")
    if parties.strip():
        lines.append(parties)
    if h.court_name or d.get("court"):
        lines.append(h.court_name or d["court"])
    when = _nice_date(d.get("next_date"))
    extra = " · ".join(x for x in (f"Next: {when}" if when else "", d.get("stage") or "", d.get("judge") or "") if x)
    if extra:
        lines.append(extra)
    if h.case_number:
        lines.append(f"CNR {h.cnr_number}")
    return lines


def _nice_date(value) -> str:
    from scraper.persist import parse_date

    d = parse_date(value)
    return d.strftime("%d %b %Y") if d else ""


def _why_unsure(h: SearchHit, others: int) -> str:
    """One line on why this candidate wasn't added by itself."""
    checks = (h.details or {}).get("checks") or {}
    if checks.get("number") is False:
        return "⚠️ Case number differs from the screenshot"
    if checks.get("date") is False:
        return "⚠️ Hearing date differs from the screenshot"
    if checks.get("parties") is False or h.score < 0.7:
        return "⚠️ Parties differ from the screenshot"
    if others:
        return "⚠️ More than one case has this number"
    if (h.details or {}).get("unchecked"):
        return "Couldn't confirm it with eCourts just now"
    return "Couldn't confirm it from the screenshot alone"


def telegram_text(job: SearchJob, hits: list[SearchHit]) -> str:
    s = summary(job, hits)
    if s.error:
        return esc(s.error)
    out = []
    if s.added:
        out.append(f"✅ *Added {len(s.added)} case{'s' if len(s.added) != 1 else ''} to your list*")
        for h in s.added[:MAX_LISTED]:
            first, *rest = _case_lines(h)
            out.append(f"• *{esc(first)}*" + "".join(f"\n  {esc(x)}" for x in rest))
        out.append(esc("You'll get reminders before every hearing. Wrong case? Tap Remove."))
        out.append("")
    if s.to_check:
        out.append(f"🔎 *Please check {'these' if len(s.to_check) > 1 else 'this'}*")
        for i, h in enumerate(s.to_check[:MAX_LISTED], 1):
            first, *rest = _case_lines(h)
            out.append(f"*{esc(f'{i}.')} {esc(first)}*" + "".join(f"\n{esc(x)}" for x in rest))
            out.append(esc(_why_unsure(h, len(s.to_check) - 1)))
        out.append(esc("Tap Add for the right one. For an exact match, send the eCourts Case History screen "
                       "(it shows the CNR)."))
        out.append("")
    if s.over_limit:
        out.append(esc(f"{s.over_limit} not added: your plan's case limit is full."))
    for m in s.missing:
        out.append(esc(f"Couldn't find {m}."))
    if s.missing:
        out.append(esc("Tip: include the grey court name at the top of the eCourts app screen."))
    if not (s.added or s.to_check or s.missing):
        out.append(esc("No matching cases found on eCourts."))
    return "\n".join(out).strip()


def telegram_keyboard(job: SearchJob, hits: list[SearchHit]) -> dict | None:
    s = summary(job, hits)
    to_check = s.to_check[:MAX_LISTED]
    buttons = [{"text": f"➕ Add {i}", "callback_data": f"fa:{job.id}:{h.id}"} for i, h in enumerate(to_check, 1)]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    # Undo for each case that was added without asking
    added_cases = job.params.get("added_cases") or {}
    for h in s.added[:MAX_LISTED]:
        case_id = added_cases.get(h.cnr_number)
        if case_id:
            rows.append([{"text": f"❌ Remove {h.case_number or h.cnr_number}"[:60],
                          "callback_data": f"rm:{job.id}:{case_id}"}])
    base = settings.APP_BASE_URL.rstrip("/")
    if base.startswith("https://"):
        rows.append([{"text": "Open on website", "url": f"{base}/find/{job.id}"}])
    return {"inline_keyboard": rows} if rows else None


def whatsapp_text(job: SearchJob, hits: list[SearchHit]) -> str:
    """Plain WhatsApp text (no buttons): unsure matches are finished on the website."""
    s = summary(job, hits)
    if s.error:
        return s.error
    out = []
    if s.added:
        out.append(f"✅ Added {len(s.added)} case{'s' if len(s.added) != 1 else ''} to your CourtPilot list:")
        out += ["• " + " · ".join(_case_lines(h)) for h in s.added[:MAX_LISTED]]
        out.append("You'll get reminders before every hearing. Wrong case? Open it on the website and tap "
                   "Stop tracking.")
    if s.to_check:
        out.append(f"\n🔎 {len(s.to_check)} possible match{'es' if len(s.to_check) != 1 else ''} need your check: "
                   f"{settings.APP_BASE_URL.rstrip('/')}/find/{job.id}")
    if s.over_limit:
        out.append(f"{s.over_limit} not added: your plan's case limit is full.")
    out += [f"Couldn't find {m}." for m in s.missing]
    if not out:
        out.append("No matching cases found on eCourts.")
    return "\n".join(out).strip()


# Names used by the first version
results_text = telegram_text
results_keyboard = telegram_keyboard


async def send_results(chat_id: str, job: SearchJob, hits: list[SearchHit]) -> None:
    await send_telegram_message(chat_id, telegram_text(job, hits), reply_markup=telegram_keyboard(job, hits))
