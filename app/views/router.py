"""
Public single-page case view — the link shared in every notification.
Only cases already in the DB are shown; this page never triggers a scrape.
"""
import html
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.message_templates import fmt_date
from config.timeutils import today_ist
from models.database import CaseSnapshot, CourtCase
from models.session import get_db
from scraper.persist import case_title, ecourts_link, normalize_cnr, parse_date

router = APIRouter(tags=["views"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _text(value):
    """eCourts returns some fields HTML-encoded ("&amp;"); decode so they aren't escaped twice."""
    return html.unescape(value) if isinstance(value, str) else value


def build_timeline(court_case: CourtCase, snapshots: list[CaseSnapshot]) -> list[dict]:
    """
    Hearing timeline, newest first. Prefers the hearing history eCourts
    returns with a CNR lookup; falls back to the dates we've observed.
    """
    raw = court_case.raw_ecourts_data if isinstance(court_case.raw_ecourts_data, dict) else {}
    entries = []
    for h in raw.get("history") or []:
        d = parse_date(h.get("business_date")) or parse_date(h.get("hearing_date"))
        if d:
            entries.append({"date": d, "purpose": _text(h.get("purpose")), "judge": _text(h.get("judge"))})

    if not entries:
        seen = set()
        for snap in snapshots:
            change = (snap.changes or {}).get("next_hearing_date") or {}
            for key in ("old", "new"):
                d = parse_date(change.get(key))
                if d and d not in seen:
                    seen.add(d)
                    entries.append({"date": d, "purpose": None, "judge": None})
        prev = court_case.previous_hearing_date
        if prev and prev not in seen:
            entries.append({"date": prev, "purpose": None, "judge": None})

    upcoming = court_case.next_hearing_date
    if upcoming and all(e["date"] != upcoming for e in entries):
        entries.append({"date": upcoming, "purpose": court_case.stage, "judge": court_case.judge})

    today = today_ist()
    for e in entries:
        e["label"] = fmt_date(e["date"])
        e["is_future"] = e["date"] >= today
        e["is_next"] = e["date"] == upcoming
    return sorted(entries, key=lambda e: e["date"], reverse=True)


@router.get("/case/{cnr_number}/view", response_class=HTMLResponse)
async def case_view(cnr_number: str, request: Request, db: AsyncSession = Depends(get_db)):
    cnr = normalize_cnr(cnr_number)
    court_case = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == cnr)) if cnr else None
    if court_case is None:
        raise HTTPException(404, "Case not found")

    snapshots = (
        await db.scalars(
            select(CaseSnapshot).where(CaseSnapshot.case_id == court_case.id).order_by(CaseSnapshot.captured_at)
        )
    ).all()
    orders = sorted(court_case.orders_json or [], key=lambda o: str(o.get("date") or ""), reverse=True)
    days_until = (court_case.next_hearing_date - today_ist()).days if court_case.next_hearing_date else None

    return templates.TemplateResponse(
        request,
        "case_view.html",
        {
            "case": court_case,
            "title": case_title(court_case),
            "ecourts_url": ecourts_link(court_case),
            "orders": orders,
            "timeline": build_timeline(court_case, list(snapshots)),
            "days_until": days_until,
            "fmt_date": fmt_date,
            "parse_date": parse_date,
        },
    )
