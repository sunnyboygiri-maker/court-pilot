"""Find a case without a CNR: name matching, result parsing, search jobs, adding results, web pages."""
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.web import find as find_web
from app.web.session import CSRF_COOKIE, SESSION_COOKIE
from app.auth.jwt import create_web_token
from models.database import CaseIndex, CourtCase, SearchHit, SearchJob, TrackedCase, UserCourt
from search import jobs, names
from search.add import add_hits
from search.ecourts import Court, Hit, SearchUnavailable, parse_results
from workers.tasks import poll_cases

FIXTURES = Path(__file__).parent / "fixtures" / "ecourts"
CSRF = "test-csrf-token"
DHORAJI = {"state_code": "17", "dist_code": "16", "complex_value": "1170073@6,22,28@N", "name": "Dhoraji, Rajkot"}


# --- Names ---

@pytest.mark.parametrize("typed", ["Jitender Kumar", "Jitendra", "Jeetender", "JITENDAR"])
def test_spellings_of_jitender_are_covered(typed):
    stems = names.search_stems(typed)
    for spelling in ["JITENDER", "JITENDRA", "JITENDAR", "JEETENDER"]:
        assert any(s in spelling for s in stems), (typed, stems, spelling)


def test_search_word_skips_titles_and_common_parts():
    assert names.pick_search_word("Smt. Lakshmi Devi") == "LAKSHMI"
    assert names.pick_search_word("Shri Vijay Singh Rathore") == "RATHORE"
    assert names.search_stems("Ram Kumar") == ["RAM KUMAR"]  # all common: search the whole phrase
    assert all(len(s) >= 3 for s in names.search_stems("Om Prakash"))


def test_skeleton_and_scores_tolerate_spelling():
    assert names.skeleton("Choudhary") == names.skeleton("Chowdhury")
    assert names.skeleton("Mohd") == names.skeleton("Mohammad")
    assert names.name_score("Jitender Kumar", "JITENDRA KUMAR") > 0.9
    assert names.name_score("Ramesh Chaudhary", "RAMESH CHOWDHURY S/O SURESH") > 0.9
    assert names.name_score("Jitender Kumar", "RAMESH KUMAR") < 0.6
    assert names.match_label(names.name_score("Kangda", "RAMABEN KANGAD")) == "Similar spelling"
    assert names.match_label(names.name_score("Kangda", "JASMINBANU KANGDA")) == "Exact name"


# --- Parsing eCourts results ---

def test_parse_party_results():
    hits = parse_results((FIXTURES / "party.html").read_text(encoding="utf-8"))
    assert [h.cnr_number for h in hits] == ["GJRJ060015282018", "GJRJ060005972018"]
    first = hits[0]
    assert first.case_number == "SPCS/33/2018" and first.case_type == "SPCS" and first.reg_year == 2018
    assert first.petitioner.startswith("AEHMAD RAZA") and first.respondent == "IKRAM HAJI YUNUS KANGDA"
    assert first.court_name == "TALUKA COURT, DHORAJI"
    assert first.extra["other_parties"] == ["AASHIYANABEN KHWAJALAL KANGDA AS A WIFE OF KHWAJALAL KANGDA"]


def test_parse_fir_results():
    hits = parse_results((FIXTURES / "fir.html").read_text(encoding="utf-8"))
    assert len(hits) == 3 and all(h.fir == "12/2019" for h in hits)
    assert hits[0].petitioner == "THE STATE OF GUJARAT" and hits[0].respondent == "SHAYARBHAI CHANDUBHAI MAKWANA"


def test_parse_advocate_results():
    hits = parse_results((FIXTURES / "advocate.html").read_text(encoding="utf-8"))
    assert len(hits) > 50
    assert hits[0].petitioner == "THE STATE OF GUJARAT" and hits[0].extra["advocates"][-1] == "C S SIROYA"


def test_parse_no_results():
    assert parse_results("<div id='nodata'>Record not found</div>") == []


def test_establishment_courts():
    assert Court("1", "2", "100@6,7@N").establishments == [""]
    assert Court("1", "2", "100@6,7@Y").establishments == ["6", "7"]


# --- Planning ---

def test_name_plan_spans_courts_years_and_spellings():
    params = {"name": "Jitender", "courts": [DHORAJI], "year_from": 2019, "year_to": 2021}
    queries = jobs.plan_queries("name", params)
    assert len(queries) == 3 * len(params["stems"])
    assert queries[0]["year"] == 2021  # newest first


def test_split_courts_are_searched_section_by_section():
    dwarka = {"state_code": "26", "dist_code": "6", "complex_value": "1260006@1,2,3,5,6@Y", "name": "Dwarka Court Complex"}
    queries = jobs.plan_queries("name", {"name": "Virendra Mehta", "courts": [dwarka], "year_from": 2021, "year_to": 2021})
    assert {q["est"] for q in queries} == {"1", "2", "3", "5", "6"}
    with pytest.raises(jobs.PlanError, match="split into 5 sections"):
        jobs.plan_queries("name", {"name": "Virendra", "courts": [dwarka], "year_from": 2000, "year_to": 2025})


def test_oversized_plan_is_trimmed_then_refused():
    many = [dict(DHORAJI, complex_value=f"{i}@1@N") for i in range(10)]
    params = {"name": "Jitender", "courts": many, "year_from": 2020, "year_to": 2025}
    assert len(jobs.plan_queries("name", params)) <= jobs.MAX_QUERIES  # fewer spellings, same coverage of years
    params = {"name": "Jitender", "courts": many, "year_from": 2010, "year_to": 2025}
    with pytest.raises(jobs.PlanError):
        jobs.plan_queries("name", params)


# --- Running searches ---

class FakeEngine:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def party(self, court, name, year, status="Both", est=None):
        self.calls.append(("party", name, year))
        if self.fail:
            raise SearchUnavailable("captcha")
        if year == 2019 and "JITEND" in name:
            return [Hit("DLWE010000012019", "CS/1/2019", "CS", 2019, "JITENDRA KUMAR", "STATE", court_name="Court 1"),
                    Hit("DLWE010000022019", "CS/2/2019", "CS", 2019, "JITENDER SINGH BHATI", "RAM LAL", court_name="Court 1")]
        if year == 2019:
            return [Hit("DLWE010000032019", "CS/3/2019", "CS", 2019, "JEETENDER KUMAR", "STATE", court_name="Court 1")]
        return []

    async def case_number(self, court, case_type, number, year):
        self.calls.append(("number", case_type, number, year))
        return [Hit("DLWE010004122024", f"CS/{number}/{year}", "CS", year, "A", "B")]

    async def fir(self, court, police_station, fir_no, year, status="Both", est=None):
        self.calls.append(("fir", police_station, fir_no, year))
        return [Hit("DLWE010007072019", "CC/707/2019", "CC", 2019, "STATE", "ACCUSED", fir=f"{fir_no}/{year}")]

    async def advocate(self, court, *, name="", bar_state="", bar_code="", bar_year="", status="Pending", est=None):
        self.calls.append(("advocate", name or bar_code))
        return [Hit(f"DLWE0100{i:04d}2023", f"CS/{i}/2023", "CS", 2023, f"CLIENT {i}", "OTHER") for i in range(1, 8)]

    async def states(self):
        return {"17": "GUJARAT"}

    async def districts(self, state):
        return {"16": "RAJKOT"}

    async def complexes(self, state, district):
        return {"1170073@6,22,28@N": "DHORAJI, RAJKOT"}

    async def case_types(self, court):
        return {"31^6": "SPCS - SPECIAL CIVIL SUIT", "10^6": "CC - CRIMINAL CASE"}

    async def police_stations(self, court):
        return {"20501-11213010": "DHORAJI POLICE STATION - RAJKOT DISTRICT 20501",
                "99999-1": "AAMLETHA POLICE STATION-NARMADA DISTRICT"}


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture(autouse=True)
def no_background_fetch(monkeypatch):
    """Auto-added cases would queue a Celery fetch; tests have no broker."""
    import search.add

    fetched = []
    monkeypatch.setattr(search.add, "fetch_details", lambda ids: fetched.extend(ids))
    return fetched


async def run(db, user, session_factory, sync_redis, engine, kind, params):
    job = await jobs.create_job(db, user, kind, params)
    await jobs.run_job(job.id, session_factory, sync_redis, engine=engine)
    await db.refresh(job)
    return job


async def test_name_search_ranks_spelling_variants(db, user, session_factory, sync_redis, engine):
    params = {"name": "Jitender Kumar", "courts": [DHORAJI], "year_from": 2018, "year_to": 2019}
    job = await run(db, user, session_factory, sync_redis, engine, "name", params)
    assert job.status == "done" and job.done == job.total
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id).order_by(SearchHit.score.desc()))).all()
    by_cnr = {h.cnr_number: h for h in hits}
    assert by_cnr["DLWE010000012019"].score > 0.9   # JITENDRA KUMAR
    assert by_cnr["DLWE010000032019"].score > 0.9   # JEETENDER KUMAR, found via another spelling
    assert by_cnr["DLWE010000022019"].score < by_cnr["DLWE010000012019"].score
    # Everything seen is remembered for next time
    assert await db.scalar(select(CaseIndex).where(CaseIndex.cnr_number == "DLWE010000032019")) is not None


async def test_repeat_search_uses_cache_and_index(db, user, session_factory, sync_redis, engine):
    params = {"name": "Jitender Kumar", "courts": [DHORAJI], "year_from": 2019, "year_to": 2019}
    await run(db, user, session_factory, sync_redis, engine, "name", params)
    calls = len(engine.calls)
    job = await run(db, user, session_factory, sync_redis, engine, "name", dict(params, year_from=2018, year_to=2019))
    # 2019 answers came from cache; only 2018 went to eCourts
    assert all(c[2] == 2018 for c in engine.calls[calls:])
    sources = set((await db.scalars(select(SearchHit.source).where(SearchHit.job_id == job.id))).all())
    assert "index" in sources


async def test_ecourts_down_marks_job_failed(db, user, session_factory, sync_redis, engine):
    engine.fail = True
    params = {"name": "Kangda", "courts": [DHORAJI], "year_from": 2018, "year_to": 2018}
    job = await run(db, user, session_factory, sync_redis, engine, "name", params)
    assert job.status == "failed" and "isn't responding" in job.error


async def test_number_fir_and_advocate_searches(db, user, session_factory, sync_redis, engine):
    job = await run(db, user, session_factory, sync_redis, engine, "number",
                    {"courts": [DHORAJI], "case_type": "31^6", "number": "412", "year": 2024})
    assert job.status == "done" and ("number", "31^6", "412", 2024) in engine.calls
    # Exactly one case for that number: added straight away
    assert job.params["added"] == ["DLWE010004122024"] and len(job.params["added_case_ids"]) == 1
    assert await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id)) is not None
    job = await run(db, user, session_factory, sync_redis, engine, "fir",
                    {"courts": [DHORAJI], "police_station": "20501-11213010", "fir_no": "707", "year": 2019})
    hit = await db.scalar(select(SearchHit).where(SearchHit.job_id == job.id))
    assert hit.fir == "707/2019"
    job = await run(db, user, session_factory, sync_redis, engine, "advocate",
                    {"courts": [DHORAJI], "advocate_name": "SIROYA", "status": "Pending"})
    assert len((await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id))).all()) == 7


# --- Adding results ---

async def test_add_hits_respects_plan_limit_and_fetches_quietly(db, user, session_factory, sync_redis, engine, scraper):
    job = await run(db, user, session_factory, sync_redis, engine, "advocate",
                    {"courts": [DHORAJI], "advocate_name": "SIROYA", "status": "Pending"})
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id))).all()
    result = await add_hits(db, user, list(hits))
    assert len(result.added) == 5 and result.over_limit == 2  # free plan: 5 cases
    again = await add_hits(db, user, list(hits[:1]))
    assert again.already and not again.added

    stub = await db.get(CourtCase, result.added[0])
    assert stub.data_hash is None and stub.petitioner.startswith("CLIENT")
    # First full fetch: details filled in, but no "case updated" alert
    from tests.conftest import make_detail

    scraper.details[stub.cnr_number] = make_detail(stub.cnr_number)
    poll = await poll_cases.poll_one(db, stub.id, scraper)
    assert poll.ok and not poll.changes
    await db.refresh(stub)
    assert stub.data_hash and stub.court_name


# --- Web ---

@pytest.fixture
async def web(client, user, engine, monkeypatch):
    client.cookies.set(SESSION_COOKIE, create_web_token(user.id))
    client.cookies.set(CSRF_COOKIE, CSRF)
    from app.main import app

    app.dependency_overrides[find_web.get_engine] = lambda: engine
    started, fetched = [], []
    monkeypatch.setattr(jobs, "start_job", lambda job_id: started.append(job_id))
    import search.add

    monkeypatch.setattr(search.add, "fetch_details", lambda ids: fetched.extend(ids))
    client.started, client.fetched = started, fetched
    return client


async def test_add_court_then_search_by_name(web, db, user, session_factory, sync_redis, engine):
    r = await web.get("/cases/new?by=name")
    assert "Choose your court" in r.text
    r = await web.get("/my-courts/add")
    assert "Gujarat" in r.text
    assert "Rajkot" in (await web.get("/find/districts", params={"state": "17"})).text
    assert "Dhoraji" in (await web.get("/find/complexes", params={"state": "17", "district": "16"})).text
    r = await web.post("/my-courts", data={"csrf_token": CSRF, "state": "17", "district": "16",
                                            "complex": "1170073@6,22,28@N", "back": "/cases/new?by=name"})
    assert r.status_code == 303 and r.headers["location"] == "/cases/new?by=name"
    court = await db.scalar(select(UserCourt).where(UserCourt.user_id == user.id))
    assert court.complex_name == "Dhoraji, Rajkot"

    r = await web.get("/cases/new?by=name")
    assert "Dhoraji, Rajkot" in r.text and "Search" in r.text
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "name", "name": "Jitender Kumar",
                                      "year_from": "2018", "year_to": "2019", "court_ids": str(court.id)})
    assert r.status_code == 303
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert web.started == [job_id]

    r = await web.get(f"/find/{job_id}")
    assert "Searching eCourts" in r.text  # still queued: progress shown, page polls itself
    await jobs.run_job(job_id, session_factory, sync_redis, engine=engine)
    r = await web.get(f"/find/{job_id}")
    assert "Best matches" in r.text and "JITENDRA KUMAR" in r.text and "Add selected" in r.text

    r = await web.post(f"/find/{job_id}/add", data={"csrf_token": CSRF, "cnr": "DLWE010000012019"})
    assert r.status_code == 303 and re.match(r"/cases/\d+/view", r.headers["location"])
    assert len(web.fetched) == 1  # full details fetched in the background
    assert "In your list" in (await web.get(f"/find/{job_id}")).text


async def test_case_number_is_the_default_tab_and_auto_adds(web, db, user, session_factory, sync_redis, engine):
    db.add(UserCourt(user_id=user.id, state_code="17", state_name="Gujarat", dist_code="16", dist_name="Rajkot",
                     complex_value="1170073@6,22,28@N", complex_name="Dhoraji"))
    await db.commit()
    court = await db.scalar(select(UserCourt))
    r = await web.get("/cases/new")
    assert "Find and add case" in r.text  # case number tab first
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "number", "case_type": "31^6",
                                      "number": "412/2024", "year": "", "court_ids": str(court.id)})
    assert r.status_code == 303
    job = await db.get(SearchJob, web.started[-1])
    assert (job.params["number"], job.params["year"]) == ("412", 2024)  # year taken from 412/2024
    await jobs.run_job(job.id, session_factory, sync_redis, engine=engine)
    r = await web.get(f"/find/{job.id}")
    assert r.status_code == 303 and re.match(r"/cases/\d+/view", r.headers["location"])


async def test_search_form_errors(web, db, user):
    db.add(UserCourt(user_id=user.id, state_code="17", state_name="Gujarat", dist_code="16", dist_name="Rajkot",
                     complex_value="1170073@6,22,28@N", complex_name="Dhoraji"))
    await db.commit()
    court = await db.scalar(select(UserCourt))
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "name", "name": "Jo", "court_ids": str(court.id)})
    assert "at least 3 letters" in r.text
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "name", "name": "Jitender"})
    assert "at least one court" in r.text
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "advocate", "advocate_by": "bar", "bar": "nonsense",
                                      "court_ids": str(court.id)})
    assert "D/1234/2015" in r.text


async def test_prefilled_bar_number_does_not_override_typed_name(web, db, user):
    """Lawyer with a bar number on file types a name and chooses 'Name': search by name."""
    user.bar_registration_no = "D/1234/2015"
    db.add(UserCourt(user_id=user.id, state_code="17", state_name="Gujarat", dist_code="16", dist_name="Rajkot",
                     complex_value="1170073@6,22,28@N", complex_name="Dhoraji"))
    await db.commit()
    assert 'value="bar" checked' in (await web.get("/cases/new?by=advocate")).text  # bar pre-selected
    court = await db.scalar(select(UserCourt))
    r = await web.post("/find", data={"csrf_token": CSRF, "kind": "advocate", "advocate_by": "name",
                                      "advocate_name": "Siroya", "bar": "D/1234/2015", "court_ids": str(court.id)})
    assert r.status_code == 303
    job = await db.scalar(select(SearchJob).where(SearchJob.kind == "advocate"))
    assert job.params["advocate_name"] == "SIROYA" and job.params["bar_code"] == ""


async def test_case_type_and_police_dropdowns(web, db, user):
    db.add(UserCourt(user_id=user.id, state_code="17", state_name="Gujarat", dist_code="16", dist_name="Rajkot",
                     complex_value="1170073@6,22,28@N", complex_name="Dhoraji"))
    await db.commit()
    court = await db.scalar(select(UserCourt))
    r = await web.get("/find/case-types", params={"court_ids": str(court.id)})
    assert 'value="31^6"' in r.text
    r = await web.get("/find/police", params={"court_ids": str(court.id)})
    assert r.text.index("Dhoraji Police") < r.text.index("Aamletha")  # this district's stations first


async def test_cannot_see_someone_elses_search(web, db, make_user, engine):
    other = await make_user(phone="+919999999999")
    job = SearchJob(user_id=other.id, kind="name", params={"name": "X", "courts": [], "year_from": 2020, "year_to": 2020})
    db.add(job)
    await db.commit()
    r = await web.get(f"/find/{job.id}")
    assert r.status_code == 404
