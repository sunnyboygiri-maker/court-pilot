"""Answer a screenshot sent on Telegram: what was found, with Add buttons."""
from bot.message_templates import esc
from config.settings import settings
from models.database import SearchHit, SearchJob
from notifications.telegram import send_telegram_message

MAX_LISTED = 8


def results_text(job: SearchJob, hits: list[SearchHit]) -> str:
    read = job.params.get("read") or []
    if job.status == "failed" and not hits:
        return esc(job.error or "We couldn't read that screenshot.")
    lines = [f"📷 *Found {len(hits)} case{'s' if len(hits) != 1 else ''}*" if hits else "📷 *No matching cases found*", ""]
    for i, h in enumerate(hits[:MAX_LISTED], 1):
        lines.append(f"*{esc(f'{i}.')} {esc(h.case_number or h.cnr_number)}*")
        parties = f"{h.petitioner or ''}" + (f" vs {h.respondent}" if h.respondent else "")
        if parties.strip():
            lines.append(esc(parties))
        if h.court_name:
            lines.append(f"_{esc(h.court_name)}_")
        if h.score < 0.7:
            lines.append(esc("⚠️ Parties differ from the screenshot: check before adding"))
        lines.append("")
    missing = [r for r in read if not r.get("found")]
    for r in missing:
        label = f"{r.get('case_type', '')} {r.get('number', '')}/{r.get('year', '')}".strip(" /") or "One case"
        why = "court not visible on the screenshot" if not r.get("court_header") else "not found on eCourts"
        lines.append(esc(f"Couldn't find {label} ({why})."))
    if missing:
        lines.append(esc("Tip: include the grey court name at the top of the eCourts app screen."))
    if len(hits) > MAX_LISTED:
        lines.append(esc(f"…and {len(hits) - MAX_LISTED} more on the website."))
    return "\n".join(lines).strip()


def results_keyboard(job: SearchJob, hits: list[SearchHit]) -> dict | None:
    buttons = [{"text": f"➕ Add {i}", "callback_data": f"fa:{job.id}:{h.id}"} for i, h in enumerate(hits[:MAX_LISTED], 1)]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    if len(hits) > 1:
        rows.append([{"text": f"✅ Add all {len(hits)}", "callback_data": f"fa:{job.id}:all"}])
    base = settings.APP_BASE_URL.rstrip("/")
    if base.startswith("https://"):
        rows.append([{"text": "Open on website", "url": f"{base}/find/{job.id}"}])
    return {"inline_keyboard": rows} if rows else None


async def send_results(chat_id: str, job: SearchJob, hits: list[SearchHit]) -> None:
    await send_telegram_message(chat_id, results_text(job, hits), reply_markup=results_keyboard(job, hits))
