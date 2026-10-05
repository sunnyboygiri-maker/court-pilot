"""
"Find a case" searches: plan, run in the background, rank, remember.

A search is a SearchJob. create_job() turns the lawyer's request into a list
of single-court, single-year eCourts queries (the portal allows nothing
wider), refusing plans too big to run politely. run_job() works through them
a few at a time under the shared eCourts rate limit, saving every case it
sees to CaseIndex and the ranked ones to SearchHit as they arrive, so the
results page can show progress live.

Runs in the `searcher` Celery worker in production, or as an asyncio task in
the API process when SEARCH_INLINE is set (local preview, tests).
"""
import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from config.settings import settings
from config.timeutils import today_ist, utcnow
from models.database import CaseIndex, SearchHit, SearchJob, User
from search import names
from search.ecourts import Court, DistrictSearch, Hit, SearchUnavailable

logger = logging.getLogger("courtpilot.search.jobs")

KINDS = ("name", "number", "fir", "advocate")
MAX_QUERIES = 80          # eCourts requests per search; a few minutes at the shared rate limit
CONCURRENCY = 3           # parallel eCourts queries per search
MIN_SCORE = 0.5           # name matches below this are noise
RESULT_CACHE_TTL = 6 * 3600
STALE_AFTER = timedelta(minutes=30)


class PlanError(ValueError):
    """The request can't be run as asked; the message is shown to the lawyer."""


def _court(d: dict) -> Court:
    return Court(d["state_code"], d["dist_code"], d["complex_value"], d.get("name", ""))


def _sections(courts: list[dict]) -> list[tuple[dict, str]]:
    """
    (court, establishment) pairs. Some complexes (e.g. Dwarka, Delhi) are split
    into several establishments that eCourts only searches one at a time, so
    each is its own query: honest progress, and one failing doesn't sink the rest.
    """
    return [(c, est) for c in courts for est in _court(c).establishments]


def plan_queries(kind: str, params: dict) -> list[dict]:
    """Each item is one eCourts request: {"op": ..., "court": {...}, "est": ..., ...}."""
    courts = params.get("courts") or []
    if kind == "screenshot":
        if not params.get("images"):
            raise PlanError("Please choose a screenshot or photo.")
    elif not courts:
        raise PlanError("Please choose at least one court.")
    sections = _sections(courts)
    if kind == "name":
        stems = params.get("stems") or names.search_stems(params["name"])
        if not stems:
            raise PlanError("Please type a name with at least 3 letters.")
        years = list(range(int(params["year_from"]), int(params["year_to"]) + 1))
        if len(sections) * len(years) > MAX_QUERIES:
            fit = max(1, MAX_QUERIES // len(sections))
            split = next((c for c in courts if len(_court(c).establishments) > 1), None)
            why = (f" {split.get('name', 'One court')} is split into {len(_court(split).establishments)} sections "
                   f"on eCourts, and each is searched separately.") if split else ""
            raise PlanError(
                f"That's too big a search for eCourts.{why} Please search up to {fit} year{'s' if fit != 1 else ''} "
                f"at a time, or fewer courts."
            )
        # Drop the rarer spellings first if the full plan is too big
        while len(stems) > 1 and len(sections) * len(years) * len(stems) > MAX_QUERIES:
            stems = stems[:-1]
        params["stems"] = stems
        return [{"op": "party", "court": c, "est": e, "name": s, "year": y}
                for y in reversed(years) for c, e in sections for s in stems]
    if kind == "number":
        return [{"op": "number", "court": courts[0], "case_type": params["case_type"],
                 "number": params["number"], "year": int(params["year"])}]
    if kind == "fir":
        return [{"op": "fir", "court": c, "est": e, "police_station": params["police_station"],
                 "fir_no": params["fir_no"], "year": int(params["year"])} for c, e in sections]
    if kind == "screenshot":
        return [{"op": "image", "key": k} for k in params.get("images", [])]
    if kind == "advocate":
        # eCourts' results don't say which cases are pending: ask for pending and
        # disposed separately so every result carries its status
        statuses = ["Pending"] if params.get("status", "Pending") == "Pending" else ["Pending", "Disposed"]
        return [{"op": "advocate", "court": c, "est": e, "name": params.get("advocate_name", ""),
                 "bar_state": params.get("bar_state", ""), "bar_code": params.get("bar_code", ""),
                 "bar_year": params.get("bar_year", ""), "status": st} for st in statuses for c, e in sections]
    raise PlanError("Unknown search")


async def create_job(db: AsyncSession, user: User, kind: str, params: dict) -> SearchJob:
    queries = plan_queries(kind, params)
    job = SearchJob(user_id=user.id, kind=kind, params=params, status="queued", total=len(queries), done=0, failed=0)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


# --- Running ---

async def _run_query(engine: DistrictSearch, q: dict) -> list[Hit]:
    court = _court(q["court"])
    op = q["op"]
    est = q.get("est")
    if op == "party":
        return await engine.party(court, q["name"], q["year"], est=est)
    if op == "number":
        return await engine.case_number(court, q["case_type"], q["number"], q["year"])
    if op == "fir":
        return await engine.fir(court, q["police_station"], q["fir_no"], q["year"], est=est)
    if op == "advocate":
        return await engine.advocate(court, name=q["name"], bar_state=q["bar_state"], bar_code=q["bar_code"],
                                     bar_year=q["bar_year"], status=q["status"], est=est)
    raise ValueError(op)


def _cache_key(q: dict) -> str:
    return "dcq:" + hashlib.sha256(json.dumps(q, sort_keys=True).encode()).hexdigest()[:32]


async def _wait_for_slot(redis) -> None:
    """Share eCourts' rate limit with the poller (workers.redis_client)."""
    from workers.redis_client import acquire_rate_slot

    while True:
        wait = acquire_rate_slot(redis, "ecourts", settings.ECOURTS_RATE_LIMIT_PER_MINUTE)
        if not wait:
            return
        await asyncio.sleep(min(wait, 10))


def score_hit(kind: str, params: dict, hit: Hit) -> float:
    if kind == "screenshot":
        return float(hit.extra.get("score", 1.0))
    if kind != "name":
        return 1.0
    query = params["name"]
    sides = [hit.petitioner, hit.respondent] + list(hit.extra.get("other_parties", []))
    main = max(names.name_score(query, s) for s in sides)
    other = (params.get("other_party") or "").strip()
    if not other:
        return main
    # The other party is on the opposite side of the "Vs"
    pet = max(names.name_score(query, hit.petitioner), *(names.name_score(query, s) for s in hit.extra.get("other_parties", [])), 0)
    opposite = hit.respondent if pet >= names.name_score(query, hit.respondent) else hit.petitioner
    return 0.7 * main + 0.3 * names.name_score(other, opposite)


async def _save(db: AsyncSession, job: SearchJob, court: dict, hits: list[Hit], source: str = "ecourts") -> None:
    if not hits:
        return
    now = utcnow()
    if source == "ecourts" and court:
        rows = [{
            "cnr_number": h.cnr_number, "case_type": h.case_type or None, "case_number": h.case_number or None,
            "reg_year": h.reg_year, "petitioner": h.petitioner or None, "respondent": h.respondent or None,
            "fir": h.fir or None, "court_name": h.court_name or None, "state_code": court["state_code"],
            "dist_code": court["dist_code"], "complex_code": court["complex_value"].split("@")[0],
            "name_key": names.name_key(" ".join([h.petitioner, h.respondent] + h.extra.get("other_parties", []))),
            "last_seen_at": now,
        } for h in {h.cnr_number: h for h in hits}.values()]
        stmt = pg_insert(CaseIndex).values(rows)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["cnr_number"],
            set_={k: getattr(stmt.excluded, k) for k in rows[0] if k != "cnr_number"},
        ))
    keep = []
    for h in hits:
        score = score_hit(job.kind, job.params, h)
        if score >= MIN_SCORE:
            keep.append({"job_id": job.id, "cnr_number": h.cnr_number, "case_type": h.case_type or None,
                         "case_number": h.case_number or None, "petitioner": h.petitioner or None,
                         "respondent": h.respondent or None, "fir": h.fir or None,
                         "court_name": h.court_name or None, "score": round(score, 3), "source": source,
                         "details": h.extra.get("details") or (
                             {"case_status": h.extra["case_status"]} if h.extra.get("case_status") else None)})
    if keep:
        stmt = pg_insert(SearchHit).values(list({r["cnr_number"]: r for r in keep}.values()))
        await db.execute(stmt.on_conflict_do_update(
            constraint="uq_search_hit",
            set_={"score": func.greatest(SearchHit.score, stmt.excluded.score), "details": stmt.excluded.details},
        ))


async def index_hits(db: AsyncSession, params: dict) -> list[Hit]:
    """Cases already in our index that may match a name search: no eCourts call, no year needed."""
    word = names.pick_search_word(params["name"])
    key = names.skeleton(word) if word else ""
    if len(key) < 2:
        return []
    courts = params.get("courts") or []
    q = select(CaseIndex).where(CaseIndex.name_key.like(f"%{key}%"))
    if courts:
        q = q.where(CaseIndex.complex_code.in_([c["complex_value"].split("@")[0] for c in courts]))
    rows = (await db.scalars(q.limit(500))).all()
    return [Hit(cnr_number=r.cnr_number, case_number=r.case_number or "", case_type=r.case_type or "",
                reg_year=r.reg_year, petitioner=r.petitioner or "", respondent=r.respondent or "",
                fir=r.fir or "", court_name=r.court_name or "") for r in rows]


async def run_job(job_id: int, session_factory: Callable, redis, engine: Optional[DistrictSearch] = None) -> None:
    engine = engine or DistrictSearch()
    async with session_factory() as db:
        job = await db.get(SearchJob, job_id)
        if job is None or job.status not in ("queued", "running"):
            return
        if job.kind == "screenshot":
            await run_screenshot_job(db, job, redis, engine)
            return
        queries = plan_queries(job.kind, dict(job.params))
        job.status, job.total = "running", len(queries)
        if job.kind == "name":
            await _save(db, job, {}, await index_hits(db, job.params), source="index")
        await db.commit()

        lock = asyncio.Lock()  # one AsyncSession: serialise its use
        sem = asyncio.Semaphore(CONCURRENCY)
        errors: list[str] = []

        async def one(q: dict) -> None:
            cached = redis.get(_cache_key(q))
            hits: Optional[list[Hit]] = None
            if cached:
                hits = [Hit(**h) for h in json.loads(cached)]
            else:
                async with sem:
                    await _wait_for_slot(redis)
                    try:
                        hits = await _run_query(engine, q)
                        redis.set(_cache_key(q), json.dumps([asdict(h) for h in hits]), ex=RESULT_CACHE_TTL)
                    except SearchUnavailable as e:
                        logger.warning("Search query failed (%s): %s", q.get("op"), e)
                        errors.append(str(e))
            async with lock:
                if hits is None:
                    job.failed += 1
                else:
                    if q["op"] == "advocate":
                        for h in hits:
                            h.extra["case_status"] = q["status"]  # "Pending" or "Disposed"
                    await _save(db, job, q["court"], hits)
                job.done += 1
                await db.commit()

        await asyncio.gather(*(one(q) for q in queries))
        job.status = "failed" if queries and job.failed == len(queries) else "done"
        if job.status == "failed":
            if any("405" in e or "Security Page" in e for e in errors):
                # eCourts' firewall refused the server itself (see scraper/proxy.py)
                logger.error("eCourts is blocking this server (405 Security Page); set ECOURTS_PROXY_URL")
                job.error = ("eCourts is refusing connections from CourtPilot at the moment. We're on it; "
                             "please try again later.")
            else:
                job.error = "eCourts isn't responding right now. Please try again in a few minutes."
        job.finished_at = utcnow()
        await db.commit()
        if job.kind == "number" and job.status == "done":
            await auto_add(db, job)


async def auto_add(db: AsyncSession, job: SearchJob) -> None:
    """
    Add the cases a search is sure about straight to the lawyer's list:
    a case-number search with exactly one result, or a screenshot case whose
    one candidate agrees with its full eCourts record. Anything less sure waits
    for a tap.
    """
    from search.add import add_hits, fetch_details

    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id))).all()
    if job.kind == "number":
        sure = list(hits) if len(hits) == 1 else []
    else:
        # Per case on the screenshot: add it only if exactly one candidate passed
        # the check against its full eCourts record (search.resolve.verify)
        by_cnr = {h.cnr_number: h for h in hits}
        sure = []
        for r in job.params.get("read", []):
            passed = [by_cnr[c] for c in r.get("found") or [] if c in by_cnr and (by_cnr[c].details or {}).get("sure")]
            if len(passed) == 1:
                sure.append(passed[0])
    if not sure:
        return
    user = await db.get(User, job.user_id)
    result = await add_hits(db, user, sure)
    fetch_details(result.new_case_ids)
    from models.database import CourtCase

    in_list = result.added + result.already
    params = dict(job.params)
    rows = (await db.execute(select(CourtCase.cnr_number, CourtCase.id).where(CourtCase.id.in_(in_list)))).all()
    params["added"] = [cnr for cnr, _ in rows]
    params["added_cases"] = {cnr: case_id for cnr, case_id in rows if case_id in result.added}
    params["added_case_ids"] = result.added
    params["over_limit"] = result.over_limit
    job.params = params
    flag_modified(job, "params")
    await db.commit()


def start_job(job_id: int) -> None:
    """Hand the job to the searcher worker (or run it here, in local preview)."""
    if settings.SEARCH_INLINE:
        from models.session import SessionLocal
        from workers.redis_client import get_sync_redis

        asyncio.get_running_loop().create_task(run_job(job_id, SessionLocal, get_sync_redis()))
    else:
        from workers.tasks.search import run_search

        run_search.delay(job_id)


async def mark_stale(db: AsyncSession, job: SearchJob) -> None:
    """A job still 'running' long after it should have finished died with its worker."""
    if job.status in ("queued", "running") and job.created_at and utcnow() - job.created_at > STALE_AFTER:
        job.status = "failed"
        job.error = "This search stopped unexpectedly. Please run it again."
        await db.commit()


def default_years() -> tuple[int, int]:
    this_year = today_ist().year
    return this_year - 4, this_year


def describe(job: SearchJob) -> str:
    p = job.params
    if job.kind == "name":
        years = f"{p['year_from']}–{p['year_to']}" if p["year_from"] != p["year_to"] else str(p["year_from"])
        other = f" vs {p['other_party']}" if p.get("other_party") else ""
        return f"“{p['name']}”{other}, {years}"
    if job.kind == "number":
        return f"{p.get('case_type_name') or 'Case'} {p['number']}/{p['year']}"
    if job.kind == "fir":
        return f"FIR {p['fir_no']}/{p['year']}, {p.get('police_station_name') or 'police station'}"
    if job.kind == "screenshot":
        n = len(p.get("read") or p.get("images") or [])
        return "Cases from your screenshot" + ("s" if n > 1 else "")
    who = p.get("advocate_name") or "/".join(x for x in (p.get("bar_state"), p.get("bar_code"), p.get("bar_year")) if x)
    return f"Cases of {who}"


def court_label(courts: list[dict]) -> str:
    if not courts:
        return ""
    first = courts[0].get("name") or "court"
    return first if len(courts) == 1 else f"{first} + {len(courts) - 1} more"


def serialize_courts(rows: list[Any]) -> list[dict]:
    """UserCourt rows -> the dicts stored in job params."""
    return [{"state_code": r.state_code, "dist_code": r.dist_code, "complex_value": r.complex_value,
             "name": court_name(r)} for r in rows]


def court_name(r: Any) -> str:
    """"Dhoraji, Rajkot" rather than "Dhoraji, Rajkot, Rajkot" when the complex already names its district."""
    return r.complex_name if r.dist_name.lower() in r.complex_name.lower() else f"{r.complex_name}, {r.dist_name}"


# --- Screenshots ---

IMAGE_TTL = 3600  # images are only kept until the search has read them
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_key() -> str:
    import secrets

    return secrets.token_urlsafe(12)


def store_image(redis, data: bytes) -> str:
    """Keep an uploaded image (sync Redis) until the searcher reads it; returns its key."""
    import base64

    key = image_key()
    redis.set(f"img:{key}", base64.b64encode(data).decode(), ex=IMAGE_TTL)
    return key


async def store_image_async(redis, data: bytes) -> str:
    import base64

    key = image_key()
    await redis.set(f"img:{key}", base64.b64encode(data).decode(), ex=IMAGE_TTL)
    return key


MAX_STATES = 3
LOOKUP_TIMEOUT = 300       # seconds for one screenshot case
SCREENSHOT_BUDGET = 900    # seconds for a whole screenshot search


async def _lawyer_places(db: AsyncSession, job: SearchJob, redis, engine: DistrictSearch):
    """
    Where this lawyer practises: (states, [(state, district)]), most used first.
    From their saved courts, then the cases they track. Keeps lookups to a
    few districts instead of whole states.
    """
    from collections import Counter

    from models.database import CourtCase, TrackedCase

    places = [(c["state_code"], c["dist_code"]) for c in job.params.get("courts", [])]
    rows = (await db.execute(select(CourtCase.state, CourtCase.district).join(TrackedCase).where(
        TrackedCase.user_id == job.user_id, CourtCase.state.isnot(None)))).all()
    if rows:
        cached = redis.get("dc:states")
        all_states = json.loads(cached) if cached else await engine.states()
        if not cached and all_states:
            redis.set("dc:states", json.dumps(all_states), ex=7 * 24 * 3600)
        by_name = {v.lower(): k for k, v in (all_states or {}).items()}
        for (state, district), _ in Counter((r.state, r.district) for r in rows).most_common():
            code = by_name.get((state or "").lower())
            if code:
                places.append((code, district) if district else (code, ""))
    states = list(dict.fromkeys(p[0] for p in places))[:MAX_STATES]
    places = [p for p in dict.fromkeys(places) if p[0] in states and p[1]]
    return states, places


async def run_screenshot_job(db: AsyncSession, job: SearchJob, redis, engine: DistrictSearch) -> None:
    import base64

    from search.resolve import parties_score, resolve
    from search.screenshot import read_image

    params = dict(job.params)
    job.status = "running"
    await db.commit()

    reads = []
    for key in params.get("images", []):
        raw = redis.get(f"img:{key}")
        redis.delete(f"img:{key}")
        if not raw:
            continue
        try:
            reads += await asyncio.to_thread(read_image, base64.b64decode(raw))
        except Exception:
            logger.exception("Couldn't read an image")
    seen, unique = set(), []
    for r in reads:
        k = r.cnr or (r.case_type.lower(), r.number, r.year)
        if k not in seen:
            seen.add(k)
            unique.append(r)
    params["read"] = [dict(r.to_dict(), found=[]) for r in unique]
    params.pop("images", None)
    job.params, job.total, job.done = params, max(1, len(unique)), 0
    await db.commit()
    if not unique:
        job.status, job.finished_at = "failed", utcnow()
        job.error = ("We couldn't read any case details from that image. Send a clear screenshot of the case "
                     "(the eCourts app's My Cases screen works best), or search by name or case number.")
        await db.commit()
        await _notify_telegram(db, job)
        return

    states, places = await _lawyer_places(db, job, redis, engine)
    started = asyncio.get_running_loop().time()
    for i, read in enumerate(unique):
        hits, section = [], None
        if asyncio.get_running_loop().time() - started > SCREENSHOT_BUDGET:
            job.failed += 1  # out of time: report what's found so far
        else:
            try:
                await _wait_for_slot(redis)
                hits, section = await asyncio.wait_for(
                    resolve(engine, redis, read, states, params.get("courts", []), places), LOOKUP_TIMEOUT)
            except Exception as e:
                logger.warning("Screenshot case lookup failed: %s", e)
                job.failed += 1
        for h in hits:
            h.extra["score"] = parties_score(read, h) or 1.0
        await _check_hits(engine, redis, read, hits)
        by_court: dict[str, tuple[dict, list[Hit]]] = {}
        for h in hits:
            court = (h.extra.get("section") or {}).get("court") or {}
            by_court.setdefault(court.get("complex_value", ""), (court, []))[1].append(h)
        for court, group in by_court.values():
            await _save(db, job, court, group, source="ecourts" if court else "screenshot")
        params["read"][i]["found"] = [h.cnr_number for h in hits]
        job.params = dict(params)
        flag_modified(job, "params")  # the change is inside a nested list
        job.done += 1
        await db.commit()
    job.status, job.finished_at = "done", utcnow()
    await db.commit()
    await auto_add(db, job)
    await _notify_telegram(db, job)


MAX_CHECKED = 3  # candidates checked against their full record (one eCourts lookup each)


async def _check_hits(engine: DistrictSearch, redis, read, hits: list[Hit]) -> None:
    """Fetch each candidate's full record and compare it with the screenshot (search.resolve.verify)."""
    from search.resolve import verify

    if not hasattr(engine, "case_details"):
        return
    for h in hits[:MAX_CHECKED]:
        try:
            await _wait_for_slot(redis)
            data = await asyncio.wait_for(engine.case_details(h.cnr_number), LOOKUP_TIMEOUT)
        except Exception as e:
            logger.warning("Couldn't check %s against eCourts: %s", h.cnr_number, e)
            h.extra["details"] = {"sure": False, "checks": {}, "unchecked": True}
            continue
        h.extra["details"] = verify(read, data)
        d = h.extra["details"]
        # A CNR-only read now has its real number, parties and court to show
        h.case_number = h.case_number or " ".join(x for x in (d.get("case_type"), d.get("case_number")) if x)
        h.petitioner = h.petitioner or d.get("petitioner") or ""
        h.respondent = h.respondent or d.get("respondent") or ""
        h.court_name = h.court_name or d.get("court") or ""
    for h in hits[MAX_CHECKED:]:
        h.extra["details"] = {"sure": False, "checks": {}, "unchecked": True}


async def _notify_telegram(db: AsyncSession, job: SearchJob) -> None:
    """Screenshots sent on Telegram or WhatsApp get their answer there."""
    chat_id = job.params.get("telegram_chat_id")
    wa_to = job.params.get("whatsapp_to")
    if not (chat_id or wa_to):
        return
    from search.telegram_reply import send_results, whatsapp_text

    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id).order_by(SearchHit.id))).all()
    try:
        if chat_id:
            await send_results(chat_id, job, list(hits))
        if wa_to:
            from notifications.whatsapp import send_whatsapp_text

            await send_whatsapp_text(wa_to, whatsapp_text(job, list(hits)))
    except Exception:
        logger.exception("Couldn't send screenshot results back to the lawyer")
