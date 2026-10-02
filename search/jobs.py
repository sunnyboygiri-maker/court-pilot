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
    if not courts:
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
    if kind == "advocate":
        return [{"op": "advocate", "court": c, "est": e, "name": params.get("advocate_name", ""),
                 "bar_state": params.get("bar_state", ""), "bar_code": params.get("bar_code", ""),
                 "bar_year": params.get("bar_year", ""), "status": params.get("status", "Pending")} for c, e in sections]
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
    if source == "ecourts":
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
                         "court_name": h.court_name or None, "score": round(score, 3), "source": source})
    if keep:
        stmt = pg_insert(SearchHit).values(list({r["cnr_number"]: r for r in keep}.values()))
        await db.execute(stmt.on_conflict_do_update(
            constraint="uq_search_hit",
            set_={"score": func.greatest(SearchHit.score, stmt.excluded.score)},
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
