"""
The parts of a district case beyond its status page: order PDFs and their
text, the judge's name and the day's cause list.

All of it is fetched in the background (searcher queue) after a poll notices
something new, and shared: a case tracked by 50 lawyers is fetched once.
"""
import io
import json
import logging
from datetime import date, timedelta
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.timeutils import today_ist
from models.database import CauseListing, CourtCase, CourtType, OrderDocument, TrackedCase
from scraper.district import DistrictPortal, complex_for_est, judge_from_court_option, parse_cause_list
from scraper.persist import parse_date

logger = logging.getLogger("courtpilot.scraper.extras")

LOOKUP_CACHE = 7 * 24 * 3600
COURTS_CACHE = 24 * 3600         # judges move; refresh the court list daily
MAX_TEXT = 20000
CAUSE_LIST_DAYS = 2              # today and tomorrow


def latest_order(court_case: CourtCase) -> Optional[dict]:
    """The newest order that can be fetched by number (district cases)."""
    numbered = [o for o in court_case.orders_json or [] if o.get("number")]
    if not numbered:
        return None
    return max(numbered, key=lambda o: (str(o.get("date") or ""), int(o["number"]) if str(o["number"]).isdigit() else 0))


def pdf_text(pdf: bytes) -> str:
    """The order's text: the PDF's own text layer, else OCR of its first pages (scanned orders)."""
    text = ""
    try:
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        logger.warning("Couldn't read the PDF's text layer")
    if len(text.strip()) >= 40:
        return _tidy(text)
    try:
        import pypdfium2

        from search.screenshot import ocr

        doc = pypdfium2.PdfDocument(pdf)
        lines = []
        for i in range(min(len(doc), 3)):
            image = doc[i].render(scale=2).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            boxes = ocr(buf.getvalue())
            lines += [t for _, _, t in sorted(boxes, key=lambda b: (round(b[1] / 15), b[0]))]
        return _tidy("\n".join(lines))
    except Exception:
        logger.warning("Couldn't OCR a scanned order")
        return _tidy(text)


def _tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_TEXT]


async def _cached(redis, key: str, ttl: int, fetch: Callable):
    hit = redis.get(key) if redis is not None else None
    if hit:
        return json.loads(hit)
    data = await fetch()
    if data and redis is not None:
        redis.set(key, json.dumps(data), ex=ttl)
    return data


async def court_option(portal: DistrictPortal, redis, ref: dict) -> tuple[Optional[str], str]:
    """(complex value, the court's cause-list option text) for a court reference."""
    complexes = await _cached(redis, f"dc:complexes:{ref['state_code']}:{ref['dist_code']}", LOOKUP_CACHE,
                              lambda: portal.complexes(ref["state_code"], ref["dist_code"])) or {}
    complex_value = complex_for_est(complexes, ref["est_code"])
    if not complex_value:
        return None, ""
    options = await _cached(redis, f"dc:courtopts:{ref['state_code']}:{ref['dist_code']}:{ref['est_code']}",
                            COURTS_CACHE, lambda: portal.court_options(ref["state_code"], ref["dist_code"],
                                                                       complex_value, ref["est_code"])) or {}
    return complex_value, options.get(f"{ref['est_code']}^{ref['court_no']}", "")


async def fetch_order(db: AsyncSession, court_case: CourtCase, number: str,
                      portal_factory: Optional[Callable] = None) -> Optional[OrderDocument]:
    """An order's PDF (from our store, else eCourts) and its text."""
    doc = await db.scalar(select(OrderDocument).where(OrderDocument.case_id == court_case.id,
                                                      OrderDocument.number == str(number)))
    if doc is not None:
        return doc
    order = next((o for o in court_case.orders_json or [] if str(o.get("number")) == str(number)), None)
    if order is None:
        return None
    async with (portal_factory or DistrictPortal)() as portal:
        page = await portal.case_page(court_case.cnr_number)
        pdf = await portal.order_pdf(page, str(number)) if page else None
    if not pdf:
        return None
    import asyncio

    doc = OrderDocument(case_id=court_case.id, number=str(number), order_date=parse_date(order.get("date")),
                        pdf=pdf, text=await asyncio.to_thread(pdf_text, pdf))
    db.add(doc)
    latest = latest_order(court_case)
    if latest and str(latest["number"]) == str(number):
        court_case.latest_order_text = doc.text
    await db.commit()
    return doc


def needs_enrich(court_case: CourtCase, old_ref: Optional[dict], stored_numbers: set[str]) -> bool:
    if court_case.court_type == CourtType.HIGH_COURT:
        return False
    if court_case.court_ref and (court_case.court_ref != old_ref or not court_case.judge_name):
        return True
    latest = latest_order(court_case)
    return bool(latest and str(latest["number"]) not in stored_numbers)


async def stored_order_numbers(db: AsyncSession, case_id: int) -> set[str]:
    return set((await db.scalars(select(OrderDocument.number).where(OrderDocument.case_id == case_id))).all())


async def enrich_case(db: AsyncSession, case_id: int, redis, portal_factory: Optional[Callable] = None) -> None:
    """Judge's name and the latest order's text for one case."""
    portal_factory = portal_factory or DistrictPortal
    court_case = await db.get(CourtCase, case_id)
    if court_case is None:
        return
    ref = court_case.court_ref
    if ref:
        try:
            async with portal_factory() as portal:
                complex_value, option = await court_option(portal, redis, ref)
                if complex_value and not court_case.court_name:
                    sections = await _cached(
                        redis, f"dc:ests:{ref['state_code']}:{ref['dist_code']}:{complex_value}", LOOKUP_CACHE,
                        lambda: portal.establishments(ref["state_code"], ref["dist_code"], complex_value)) or {}
                    court_case.court_name = sections.get(ref["est_code"]) or None
            name = judge_from_court_option(option)
            if name:
                court_case.judge_name = name
            await db.commit()
        except Exception:
            logger.warning("Couldn't look up the judge for %s", court_case.cnr_number)
    latest = latest_order(court_case)
    if latest and str(latest["number"]) not in await stored_order_numbers(db, case_id):
        try:
            await fetch_order(db, court_case, str(latest["number"]), portal_factory)
        except Exception:
            logger.warning("Couldn't fetch the latest order of %s", court_case.cnr_number)


def _criminal(court_case: CourtCase) -> list[bool]:
    """Which cause list (criminal/civil) to read first for this case type."""
    kind = (court_case.case_type or "").lower()
    first = any(w in kind for w in ("cr", "ct. case", "bail", "ndps", "sessions", "fir", "complaint", "138"))
    return [first, not first]


async def refresh_cause_lists(db: AsyncSession, redis, portal_factory: Optional[Callable] = None,
                              on: Optional[date] = None, case_ids: Optional[list[int]] = None) -> int:
    """
    Read today's and tomorrow's cause lists for every court that has a tracked
    case listed then: one list per court and day, however many cases it has.
    Returns how many cases were found on a list.
    """
    portal_factory = portal_factory or DistrictPortal
    start = on or today_ist()
    days = [start + timedelta(days=i) for i in range(CAUSE_LIST_DAYS)]
    q = (select(CourtCase).where(CourtCase.id.in_(select(TrackedCase.case_id)),
                                 CourtCase.next_hearing_date.in_(days), CourtCase.court_ref.isnot(None)))
    if case_ids:
        q = q.where(CourtCase.id.in_(case_ids))
    cases = (await db.scalars(q)).all()
    groups: dict[tuple, list[CourtCase]] = {}
    for c in cases:
        r = c.court_ref
        groups.setdefault((r["state_code"], r["dist_code"], r["est_code"], r["court_no"], c.next_hearing_date), []).append(c)
    found = 0
    for (state, dist, est, court_no, day), members in groups.items():
        ref = {"state_code": state, "dist_code": dist, "est_code": est, "court_no": court_no}
        try:
            async with portal_factory() as portal:
                complex_value, option = await court_option(portal, redis, ref)
                if not (complex_value and option):
                    continue
                wanted = {c.cnr_number: c for c in members}
                lists = []
                for criminal in _criminal(members[0]):
                    parsed = parse_cause_list(await portal.cause_list(ref, complex_value, option,
                                                                      day.strftime("%d-%m-%Y"), criminal))
                    lists.append(parsed)
                    if wanted.keys() <= {e["cnr"] for e in parsed["entries"]}:
                        break
        except Exception:
            logger.warning("Couldn't read the cause list for court %s on %s", court_no, day)
            continue
        published = any(p["entries"] for p in lists)
        for cnr, c in wanted.items():
            entry, meta = next(((e, p) for p in lists for e in p["entries"] if e["cnr"] == cnr), (None, lists[0] if lists else {}))
            if entry is None and not published:
                continue  # not published yet: check again later
            listing = await db.scalar(select(CauseListing).where(CauseListing.case_id == c.id,
                                                                 CauseListing.listing_date == day))
            if listing is None:
                listing = CauseListing(case_id=c.id, listing_date=day)
                db.add(listing)
            listing.serial = entry["serial"] if entry else None
            listing.purpose = entry["purpose"] if entry else None
            listing.category = entry["category"] if entry else None
            listing.judge = meta.get("judge") or None
            listing.vc_url = meta.get("vc_url") or None
            if meta.get("judge") and not c.judge_name:
                c.judge_name = meta["judge"]
            found += bool(entry)
        await db.commit()
    return found


def request_cause_list(case_id: int) -> None:
    """Look for this case in its court's cause list now (opened its page shortly before the hearing)."""
    from config.settings import settings

    if settings.SEARCH_INLINE:
        import asyncio

        async def run():
            from models.session import SessionLocal
            from workers.redis_client import get_sync_redis

            async with SessionLocal() as db:
                await refresh_cause_lists(db, get_sync_redis(), case_ids=[case_id])

        asyncio.get_running_loop().create_task(run())
        return
    from workers.tasks.search import refresh_cause_lists as task

    task.delay([case_id])


def request_enrich(case_id: int) -> None:
    """enrich_case in the background (searcher worker; inline in the local preview)."""
    from config.settings import settings

    if settings.SEARCH_INLINE:
        import asyncio

        async def run():
            from models.session import SessionLocal
            from workers.redis_client import get_sync_redis

            async with SessionLocal() as db:
                await enrich_case(db, case_id, get_sync_redis())

        asyncio.get_running_loop().create_task(run())
        return
    from workers.tasks.search import enrich_case as task

    task.delay(case_id)
