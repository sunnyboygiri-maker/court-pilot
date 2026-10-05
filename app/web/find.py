"""
"Add a case" hub: by CNR, case number, party name, FIR, or importing an
advocate's cases; plus "My courts" and the live results page.

Searches run in the background (search.jobs); the results page polls itself
with htmx every two seconds until the search is done.
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import service
from app.redis import get_redis
from app.web.session import get_web_user, is_htmx, redirect, render, verify_csrf
from config.timeutils import today_ist
from models.database import CourtCase, SearchHit, SearchJob, TrackedCase, User, UserCourt
from models.session import get_db
from search import jobs
from search.ecourts import Court, DistrictSearch, SearchUnavailable
from search.names import match_label

logger = logging.getLogger("courtpilot.web.find")

router = APIRouter(include_in_schema=False)

# Lawyers mostly know the case number, so that comes first
TABS = [("number", "Case number"), ("cnr", "CNR"), ("screenshot", "Screenshot"), ("name", "Party name"),
        ("fir", "FIR"), ("advocate", "My cases")]
MAX_UPLOADS = 10
DROPDOWN_TTL = 7 * 24 * 3600
BAR_RE = re.compile(r"^\s*([A-Za-z]{1,4})\s*/\s*(\d{1,6})\s*/\s*(\d{4})\s*$")


def get_engine() -> DistrictSearch:
    return DistrictSearch()


async def cached(redis: Redis, key: str, fetch) -> dict:
    """eCourts dropdowns change rarely: keep them a week."""
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    data = await fetch()
    if data:
        await redis.set(key, json.dumps(data), ex=DROPDOWN_TTL)
    return data


def options_html(options: dict, placeholder: str, selected: str = "") -> str:
    from html import escape

    out = [f'<option value="">{escape(placeholder)}</option>']
    for value, label in sorted(options.items(), key=lambda kv: kv[1]):
        sel = " selected" if value == selected else ""
        out.append(f'<option value="{escape(value)}"{sel}>{escape(label.title() if label.isupper() else label)}</option>')
    return "".join(out)


async def my_courts(db: AsyncSession, user: User) -> list[UserCourt]:
    return list((await db.scalars(select(UserCourt).where(UserCourt.user_id == user.id).order_by(UserCourt.id))).all())


def court_of(uc: UserCourt) -> Court:
    return Court(uc.state_code, uc.dist_code, uc.complex_value, jobs.court_name(uc))


def parse_bar(value: Optional[str]) -> tuple[str, str, str]:
    m = BAR_RE.match(value or "")
    return (m.group(1).upper(), m.group(2), m.group(3)) if m else ("", "", "")


# --- Hub ---

async def hub_context(db: AsyncSession, user: User, by: str, **extra) -> dict:
    from app.web.router import page_context

    ctx = await page_context(db, user, "add")
    year_from, year_to = jobs.default_years()
    this_year = today_ist().year
    bar_state, bar_code, bar_year = parse_bar(user.bar_registration_no)
    ctx.update(
        by=by if by in dict(TABS) else "number",
        tabs=TABS,
        courts=await my_courts(db, user),
        years=list(range(this_year, this_year - 30, -1)),
        form={"year_from": year_from, "year_to": year_to, "year": this_year,
              "advocate_name": re.sub(r"^(adv\.?|advocate)\s+", "", user.name or "", flags=re.I),
              "bar": "/".join(x for x in (bar_state, bar_code, bar_year) if x),
              "advocate_by": "bar" if bar_code else "name"},
    )
    ctx.update(extra)
    return ctx


@router.get("/cases/new")
async def add_case_page(request: Request, by: str = "number", cnr: str = "",
                        user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    ctx = await hub_context(db, user, by)
    ctx["form"]["cnr"] = cnr
    return render(request, "web/case_new.html", ctx)


# --- My courts ---

@router.get("/find/districts", response_class=HTMLResponse)
async def districts_options(state: str = "", redis: Redis = Depends(get_redis), engine: DistrictSearch = Depends(get_engine),
                            user: User = Depends(get_web_user)):
    if not state:
        return options_html({}, "Choose a state first")
    try:
        data = await cached(redis, f"dc:districts:{state}", lambda: engine.districts(state))
    except Exception:
        logger.exception("districts")
        return options_html({}, "eCourts isn't responding. Try again")
    return options_html(data, "Choose district")


@router.get("/find/complexes", response_class=HTMLResponse)
async def complexes_options(state: str = "", district: str = "", redis: Redis = Depends(get_redis),
                            engine: DistrictSearch = Depends(get_engine), user: User = Depends(get_web_user)):
    if not (state and district):
        return options_html({}, "Choose a district first")
    try:
        data = await cached(redis, f"dc:complexes:{state}:{district}", lambda: engine.complexes(state, district))
    except Exception:
        logger.exception("complexes")
        return options_html({}, "eCourts isn't responding. Try again")
    return options_html(data, "Choose court complex")


@router.get("/my-courts/add")
async def add_court_page(request: Request, back: str = "", user: User = Depends(get_web_user),
                         db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                         engine: DistrictSearch = Depends(get_engine)):
    from app.web.router import page_context

    ctx = await page_context(db, user, "add")
    try:
        states = await cached(redis, "dc:states", engine.states)
    except Exception:
        logger.exception("states")
        states = {}
    ctx.update(states_html=options_html(states, "Choose state"), back=back if back.startswith("/") else "/cases/new?by=name",
               courts=await my_courts(db, user))
    return render(request, "web/court_add.html", ctx)


@router.post("/my-courts", dependencies=[Depends(verify_csrf)])
async def add_court(request: Request, state: str = Form(""), district: str = Form(""), complex: str = Form(""),
                    back: str = Form("/cases/new?by=name"), user: User = Depends(get_web_user),
                    db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                    engine: DistrictSearch = Depends(get_engine)):
    back = back if back.startswith("/") else "/cases/new?by=name"
    try:
        states = await cached(redis, "dc:states", engine.states)
        dists = await cached(redis, f"dc:districts:{state}", lambda: engine.districts(state)) if state else {}
        cxs = await cached(redis, f"dc:complexes:{state}:{district}", lambda: engine.complexes(state, district)) if district else {}
    except Exception:
        return redirect(request, f"/my-courts/add?back={back}", "eCourts isn't responding right now. Please try again.")
    if state not in states or district not in dists or complex not in cxs:
        return redirect(request, f"/my-courts/add?back={back}", "Please choose a state, district and court complex.")
    db.add(UserCourt(user_id=user.id, state_code=state, state_name=states[state].title(),
                     dist_code=district, dist_name=dists[district].title(),
                     complex_value=complex, complex_name=cxs[complex].title()))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return redirect(request, back, "That court is already in My courts.")
    return redirect(request, back, f"Added {cxs[complex].title()} to My courts.")


@router.post("/my-courts/{court_id}/remove", dependencies=[Depends(verify_csrf)])
async def remove_court(request: Request, court_id: int, back: str = Form("/settings#courts"),
                       user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    uc = await db.get(UserCourt, court_id)
    if uc is not None and uc.user_id == user.id:
        await db.delete(uc)
        await db.commit()
    return redirect(request, back if back.startswith("/") else "/settings#courts", "Court removed from My courts.")


# --- Per-court dropdowns for the search forms ---

async def _user_court(db: AsyncSession, user: User, court_id: str) -> Optional[UserCourt]:
    try:
        uc = await db.get(UserCourt, int(court_id))
    except (TypeError, ValueError):
        return None
    return uc if uc is not None and uc.user_id == user.id else None


@router.get("/find/case-types", response_class=HTMLResponse)
async def case_type_options(court_ids: list[str] = Query(default=[]), user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db),
                            redis: Redis = Depends(get_redis), engine: DistrictSearch = Depends(get_engine)):
    uc = await _user_court(db, user, court_ids[0] if court_ids else "")
    if uc is None:
        return options_html({}, "Choose a court first")
    court = court_of(uc)

    async def fetch():
        if court.establishments == [""]:
            return await engine.case_types(court)
        # A split complex (e.g. Tis Hazari): each section has its own case types
        sections = await engine.establishments(court)
        out = {}
        for est, est_name in sections.items():
            for code, label in (await engine.case_types(court, est)).items():
                out[code] = f"{label} · {est_name}"
        return out

    try:
        data = await cached(redis, f"dc:casetypes-all:{uc.state_code}:{uc.dist_code}:{uc.complex_value}", fetch)
    except Exception:
        logger.exception("case types")
        return options_html({}, "eCourts isn't responding. Try again")
    return options_html(data, "Choose case type")


@router.get("/find/police", response_class=HTMLResponse)
async def police_options(court_ids: list[str] = Query(default=[]), user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db),
                         redis: Redis = Depends(get_redis), engine: DistrictSearch = Depends(get_engine)):
    uc = await _user_court(db, user, court_ids[0] if court_ids else "")
    if uc is None:
        return options_html({}, "Choose a court first")
    try:
        data = await cached(redis, f"dc:police:{uc.state_code}:{uc.dist_code}:{uc.complex_value}",
                            lambda: engine.police_stations(court_of(uc)))
    except Exception:
        logger.exception("police stations")
        return options_html({}, "eCourts isn't responding. Try again")
    # Stations of this district first: the list covers the whole state
    district = uc.dist_name.upper()
    ordered = dict(sorted(data.items(), key=lambda kv: (district not in kv[1].upper(), kv[1])))
    from html import escape

    return '<option value="">Choose police station</option>' + "".join(
        f'<option value="{escape(v)}">{escape(label.title())}</option>' for v, label in ordered.items())


# --- Starting a search ---

@router.post("/find", dependencies=[Depends(verify_csrf)])
async def start_search(
    request: Request,
    kind: str = Form(""),
    court_ids: list[str] = Form(default=[]),
    name: str = Form(""),
    other_party: str = Form(""),
    year_from: int = Form(0),
    year_to: int = Form(0),
    case_type: str = Form(""),
    number: str = Form(""),
    year: str = Form(""),
    police_station: str = Form(""),
    fir_no: str = Form(""),
    advocate_name: str = Form(""),
    bar: str = Form(""),
    advocate_by: str = Form("name"),
    status: str = Form("Pending"),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    # Empty or non-numeric year boxes are treated as "not given", never a server error
    year = int(year) if str(year).strip().isdigit() else 0
    form = {"name": name, "other_party": other_party, "year_from": year_from, "year_to": year_to,
            "case_type": case_type, "number": number, "year": year, "police_station": police_station,
            "fir_no": fir_no, "advocate_name": advocate_name, "bar": bar, "advocate_by": advocate_by, "status": status,
            "court_ids": court_ids}
    picked = [uc for uc in await my_courts(db, user) if str(uc.id) in court_ids]
    params: dict = {"courts": jobs.serialize_courts(picked)}
    this_year = today_ist().year
    error = None
    if kind == "name":
        name = name.strip()
        lo, hi = sorted((year_from or this_year, year_to or this_year))
        params.update(name=name, other_party=other_party.strip(), year_from=lo, year_to=hi)
        if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
            error = "Please type at least 3 letters of the name."
    elif kind == "number":
        # "714/2018" or "714 / 2018" in the number box: take the year from it
        m = re.match(r"^\s*(\d+)\s*/\s*(\d{4})\s*$", number)
        if m:
            number, year = m.group(1), year or int(m.group(2))
        params.update(case_type=case_type, number=re.sub(r"\D", "", number), year=year)
        if not case_type:
            error = "Please choose the case type."
        elif not params["number"]:
            error = "Please enter the case number."
        elif not 1950 <= year <= this_year:
            error = "Please enter the year the case was registered."
        else:
            types = await redis.get(f"dc:casetypes-all:{picked[0].state_code}:{picked[0].dist_code}:{picked[0].complex_value}") if picked else None
            params["case_type_name"] = (json.loads(types).get(case_type, "") if types else "").title()
        params["courts"] = params["courts"][:1]
    elif kind == "fir":
        params.update(police_station=police_station, fir_no=re.sub(r"\D", "", fir_no), year=year)
        if not police_station:
            error = "Please choose the police station."
        elif not params["fir_no"]:
            error = "Please enter the FIR number."
        elif not 1950 <= year <= this_year:
            error = "Please enter the FIR year."
        elif picked:
            stations = await redis.get(f"dc:police:{picked[0].state_code}:{picked[0].dist_code}:{picked[0].complex_value}")
            params["police_station_name"] = (json.loads(stations).get(police_station, "") if stations else "").title()
    elif kind == "advocate":
        # One or the other, as the lawyer chose: a pre-filled bar number must
        # never silently override a name they typed
        by_bar = advocate_by == "bar"
        bar_state, bar_code, bar_year = parse_bar(bar) if by_bar else ("", "", "")
        params.update(advocate_name="" if by_bar else advocate_name.strip().upper(), bar_state=bar_state,
                      bar_code=bar_code, bar_year=bar_year, status="Both" if status == "Both" else "Pending")
        if by_bar and not bar_code:
            error = "Enter the bar registration number like D/1234/2015."
        elif not by_bar and len(re.sub(r"[^A-Za-z]", "", advocate_name)) < 3:
            error = "Please enter your name as it appears on eCourts (at least 3 letters)."
    else:
        error = "Please choose how to search."
    if not error and not picked:
        error = "Please choose at least one court."

    if not error:
        try:
            job = await jobs.create_job(db, user, kind, params)
        except jobs.PlanError as e:
            error = str(e)
    if error:
        ctx = await hub_context(db, user, kind, error=error)
        ctx["form"].update(form)
        return render(request, "web/case_new.html", ctx)
    jobs.start_job(job.id)
    return redirect(request, f"/find/{job.id}")


@router.post("/find/screenshot", dependencies=[Depends(verify_csrf)])
async def start_screenshot_search(
    request: Request,
    images: list[UploadFile] = File(default=[]),
    user: User = Depends(get_web_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    keys, error = [], None
    files = [f for f in images if f.filename]
    if not files:
        error = "Please choose a screenshot or photo of the case."
    elif len(files) > MAX_UPLOADS:
        error = f"Please send up to {MAX_UPLOADS} images at a time."
    for f in files if not error else []:
        data = await f.read()
        if not (f.content_type or "").startswith("image/"):
            error = f"{f.filename} isn't an image. Please send screenshots or photos."
            break
        if len(data) > jobs.MAX_IMAGE_BYTES:
            error = f"{f.filename} is too large (max 10 MB)."
            break
        keys.append(await jobs.store_image_async(redis, data))
    if error:
        ctx = await hub_context(db, user, "screenshot", error=error)
        return render(request, "web/case_new.html", ctx)
    job = await jobs.create_job(db, user, "screenshot", {
        "images": keys, "courts": jobs.serialize_courts(await my_courts(db, user)),
    })
    jobs.start_job(job.id)
    return redirect(request, f"/find/{job.id}")


# --- Results ---

async def _job_for(db: AsyncSession, user: User, job_id: int) -> Optional[SearchJob]:
    job = await db.get(SearchJob, job_id)
    return job if job is not None and job.user_id == user.id else None


ADVOCATE_FILTERS = {"all": "All", "pending": "Pending", "disposed": "Disposed"}


@router.get("/find/{job_id}")
async def results_page(request: Request, job_id: int, show: str = "all", user: User = Depends(get_web_user),
                       db: AsyncSession = Depends(get_db)):
    from app.web.router import page_context

    job = await _job_for(db, user, job_id)
    if job is None:
        ctx = await page_context(db, user, "add")
        return render(request, "web/not_found.html", ctx, status_code=404)
    await jobs.mark_stale(db, job)
    await db.refresh(job)
    added_ids = job.params.get("added_case_ids") or []
    if job.kind == "number" and job.status == "done" and len(added_ids) == 1:
        # Exact case number, one result: it was added automatically; go straight to it
        response = redirect(request, f"/cases/{added_ids[0]}/view", "Case added. We'll remind you before every hearing.")
        if is_htmx(request):
            response.headers["HX-Redirect"] = f"/cases/{added_ids[0]}/view"
            response.status_code = 200
        return response
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id)
                             .order_by(SearchHit.score.desc(), SearchHit.case_number))).all()
    tracked = set((await db.scalars(
        select(SearchHit.cnr_number)
        .join(CourtCase, CourtCase.cnr_number == SearchHit.cnr_number)
        .join(TrackedCase, TrackedCase.case_id == CourtCase.id)
        .where(SearchHit.job_id == job.id, TrackedCase.user_id == user.id)
    )).all())
    # Advocate searches: count by status and filter with All / Pending / Disposed
    def case_status(h) -> str:
        return ((h.details or {}).get("case_status") or "").lower()

    status_counts = {"all": len(hits), "pending": sum(case_status(h) == "pending" for h in hits),
                     "disposed": sum(case_status(h) == "disposed" for h in hits)}
    show = show if show in ADVOCATE_FILTERS else "all"
    if job.kind == "advocate" and show != "all":
        hits = [h for h in hits if case_status(h) == show]
    ctx = await page_context(db, user, "add")
    running = job.status in ("queued", "running")
    ctx.update(
        show=show, status_counts=status_counts, filters=ADVOCATE_FILTERS,
        disposed_searched=job.params.get("status", "Pending") != "Pending",
        job=job, running=running, hits=hits, tracked=tracked,
        title=jobs.describe(job),
        courts_label=("Read from your screenshot, then looked up on eCourts" if job.kind == "screenshot"
                      else jobs.court_label(job.params.get("courts", []))),
        read=job.params.get("read") or [],
        strong=[h for h in hits if h.score >= 0.88], other=[h for h in hits if h.score < 0.88],
        label=match_label, pct=int(100 * job.done / job.total) if job.total else 0,
        slots_left=max(0, service.case_limit(user) - await service.count_tracked(db, user.id)),
    )
    return render(request, "web/find_results.html", ctx)


@router.post("/find/{job_id}/add", dependencies=[Depends(verify_csrf)])
async def add_from_results(request: Request, job_id: int, cnr: list[str] = Form(default=[]),
                           user: User = Depends(get_web_user), db: AsyncSession = Depends(get_db)):
    from search.add import add_hits, fetch_details

    job = await _job_for(db, user, job_id)
    if job is None:
        return redirect(request, "/cases/new")
    if not cnr:
        return redirect(request, f"/find/{job_id}", "Tick the cases you want to add.")
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id,
                                                      SearchHit.cnr_number.in_(cnr)))).all()
    result = await add_hits(db, user, list(hits))
    fetch_details(result.new_case_ids)
    parts = []
    if result.added:
        parts.append(f"Added {len(result.added)} case{'s' if len(result.added) != 1 else ''}.")
    if result.already:
        parts.append(f"{len(result.already)} already in your list.")
    if result.over_limit:
        parts.append(f"{result.over_limit} not added: your plan's case limit is full.")
    message = " ".join(parts) or "Nothing added."
    if len(result.added) == 1 and len(cnr) == 1:
        return redirect(request, f"/cases/{result.added[0]}/view", "Case added. Full details will appear in a minute.")
    return redirect(request, "/" if result.added else f"/find/{job_id}", message)
