"""Screenshots -> cases: reading eCourts-app screens, matching courts, the search job, Telegram and the web tab."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.auth.jwt import create_web_token
from app.web.session import CSRF_COOKIE, SESSION_COOKIE
from bot import telegram_bot as tb
from models.database import SearchHit, SearchJob, TrackedCase, UserCourt
from search import jobs, resolve as resolve_mod, screenshot
from search.ecourts import Hit
from search.screenshot import ReadCase, read_cases
from search.telegram_reply import results_keyboard, results_text
from tests.test_bot_and_templates import assert_valid_markdown_v2, make_update, replies

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
CSRF = "test-csrf-token"


@pytest.fixture(autouse=True)
def bot_db(monkeypatch, session_factory):
    """Bot handlers open their own sessions: point them at the test database."""
    monkeypatch.setattr(tb, "SessionLocal", session_factory)


def ocr_items(name: str):
    return [tuple(i) for i in json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))]


# --- Reading screenshots (OCR output recorded from real eCourts-app screenshots, names changed) ---

@pytest.mark.parametrize("fixture,expected", [
    ("dwarka_ct_case", ("Ct. Cases", "4412345", "2016", "RAHUL VERMA", "SURESH YADAV",
                        "Chief Metropolitan Magistrate, South-West, DWK,South West", "Feb 04 2026")),
    ("karkardooma_cs", ("CS", "614", "2018", "RAHUL VERMA", "SURESH YADAV",
                        "District and Sessions Judge, East, KKD,East", "Sep 28 2026")),
    ("tis_hazari_cr_case", ("Cr. Case", "65303", "2016", "STATE", "NARESH PAL SHARMA",
                            "Chief Metropolitan Magistrate, West, THC,West", "Aug 14 2026")),
    ("cs_dj_adj_no_header", ("CS DJ ADJ", "812", "2021", "RAHUL VERMA", "MAHESH SINGH GILL AND ORS", "", "Feb 21 2026")),
])
def test_reads_ecourts_app_my_cases_screen(fixture, expected):
    cases = read_cases(ocr_items(fixture))
    assert len(cases) == 1
    c = cases[0]
    assert (c.case_type, c.number, c.year, c.petitioner, c.respondent, c.court_header, c.next_date) == expected
    assert c.status == "Pending"


def test_reads_cnr_anywhere_and_generic_text():
    items = [(10, 10, "Case Status"), (10, 40, "CNR Number : DLWE010012342024"), (10, 80, "CS/412/2024")]
    assert read_cases(items)[0].cnr == "DLWE010012342024"
    items = [(10, 10, "Order dated 01.02.2024"), (10, 40, "CS/412/2024"), (10, 70, "RAM LAL Vs SHYAM LAL")]
    c = read_cases(items)[0]
    assert (c.case_type, c.number, c.year, c.petitioner, c.respondent) == ("CS", "412", "2024", "RAM LAL", "SHYAM LAL")
    assert read_cases([(1, 1, "a photo of a cat")]) == []


# --- Matching the court and finding the case ---

class Engine:
    """eCourts as seen by search.resolve: two Delhi districts, one split complex."""

    def __init__(self):
        self.searched = []

    async def districts(self, state):
        return {"8": "WEST", "6": "SOUTH WEST"}

    async def complexes(self, state, dist):
        return {"8": {"1260001@1,2@Y": "Tis Hazari Court Complex"},
                "6": {"1260006@1,2@Y": "Dwarka Court Complex"}}[dist]

    async def establishments(self, court):
        if court.complex_code == "1260001":
            return {"1": "District and Sessions Judge, West, THC", "2": "Chief Metropolitan Magistrate, West, THC"}
        return {"1": "District and Session Judge, South-West DWK", "2": "Chief Metropolitan Magistrate, South-West DWK"}

    async def case_types(self, court, est=None):
        if est == "2":
            return {"21^2": "Cr. Case - CRIMINAL CASE", "22^2": "Ct. Cases - COMPLAINT CASES"}
        return {"5^1": "CS DJ ADJ - CIVIL SUIT", "6^1": "CS - CIVIL SUIT"}

    async def case_number(self, court, code, number, year):
        self.searched.append((court.complex_code, code, number, year))
        if (court.complex_code, code, number) == ("1260001", "21^2", "65303"):
            return [Hit("DLWT020142452016", f"Cr. Case/{number}/{year}", "Cr. Case", year, "STATE", "NARESH PAL SHARMA")]
        if (court.complex_code, code, number) == ("1260006", "5^1", "812"):
            return [Hit("DLSW010079232021", f"CS DJ ADJ/{number}/{year}", "CS DJ ADJ", year, "RAHUL VERMA", "MAHESH SINGH GILL")]
        return []

    async def states(self):
        return {"26": "Delhi"}


async def test_court_header_picks_the_right_section(sync_redis):
    engine = Engine()
    read = read_cases(ocr_items("tis_hazari_cr_case"))[0]
    hits, section = await resolve_mod.resolve(engine, sync_redis, read, ["26"], [])
    assert hits[0].cnr_number == "DLWT020142452016"
    assert section["name"].startswith("Chief Metropolitan Magistrate, West, THC")
    assert engine.searched == [("1260001", "21^2", "65303", 2016)]  # straight to the right section


async def test_missing_header_tries_sections_with_that_case_type(sync_redis):
    engine = Engine()
    read = read_cases(ocr_items("cs_dj_adj_no_header"))[0]
    assert (await resolve_mod.resolve(engine, sync_redis, read, ["26"], []))[0] == []  # no idea where to look
    hits, section = await resolve_mod.resolve(engine, sync_redis, read, ["26"], [], places=[("26", "8"), ("26", "South West")])
    assert hits[0].cnr_number == "DLSW010079232021"
    assert all(code == "5^1" for _, code, _, _ in engine.searched)  # only courts that have "CS DJ ADJ"


async def test_wrong_parties_are_not_offered(sync_redis):
    engine = Engine()
    read = ReadCase(court_header="Chief Metropolitan Magistrate, West, THC", case_type="Cr. Case", number="65303",
                    year="2016", petitioner="STATE", respondent="SOMEONE ELSE ENTIRELY")
    hits, _ = await resolve_mod.resolve(engine, sync_redis, read, ["26"], [])
    assert hits == []


def test_header_names_the_district():
    districts = {"8": "WEST", "6": "SOUTH WEST", "2": "EAST"}
    assert resolve_mod.header_districts("Chief Metropolitan Magistrate, South-West, DWK,South West", districts) == ["6"]
    assert resolve_mod.header_districts("Chief Metropolitan Magistrate, West, THC,West", districts) == ["8"]
    assert resolve_mod.header_districts("District and Sessions Judge, East, KKD,East", districts) == ["2"]


def test_case_type_matching():
    types = {"21^2": "Cr. Case - CRIMINAL CASE", "5^1": "CS DJ ADJ - CIVIL SUIT", "6^1": "CS - CIVIL SUIT"}
    assert resolve_mod.best_case_type(types, "Cr. Case") == ["21^2"]
    assert resolve_mod.best_case_type(types, "CS") == ["6^1"]
    assert resolve_mod.best_case_type(types, "CS DJ ADJ") == ["5^1"]


# --- The search job ---

async def run_screenshot(db, user, session_factory, sync_redis, reads, monkeypatch, **params):
    monkeypatch.setattr(screenshot, "read_image", lambda data: reads)
    key = jobs.store_image(sync_redis, b"fake image bytes")
    job = await jobs.create_job(db, user, "screenshot", {"images": [key], "courts": [], **params})
    await jobs.run_job(job.id, session_factory, sync_redis, engine=Engine())
    await db.refresh(job)
    return job


async def test_screenshot_job_finds_cases_and_reports_misses(db, user, session_factory, sync_redis, monkeypatch):
    reads = [read_cases(ocr_items("tis_hazari_cr_case"))[0],
             ReadCase(court_header="Some Court Nobody Has", case_type="CS", number="1", year="2020")]
    db.add(UserCourt(user_id=user.id, state_code="26", state_name="Delhi", dist_code="8", dist_name="West",
                     complex_value="1260001@1,2@Y", complex_name="Tis Hazari"))
    await db.commit()
    job = await run_screenshot(db, user, session_factory, sync_redis, reads, monkeypatch,
                               courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "Tis Hazari"}])
    assert job.status == "done" and job.done == 2
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id))).all()
    assert [h.cnr_number for h in hits] == ["DLWT020142452016"]
    assert [bool(r["found"]) for r in job.params["read"]] == [True, False]
    assert sync_redis.keys("img:*") == []  # image deleted once read


async def test_unreadable_image_fails_with_advice(db, user, session_factory, sync_redis, monkeypatch):
    job = await run_screenshot(db, user, session_factory, sync_redis, [], monkeypatch)
    assert job.status == "failed" and "couldn't read" in job.error


# --- Telegram ---

async def test_results_message_and_buttons(db, user, session_factory, sync_redis, monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="MarkdownV2", reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    import search.telegram_reply as tr

    monkeypatch.setattr(tr, "send_telegram_message", fake_send)
    reads = [read_cases(ocr_items("tis_hazari_cr_case"))[0], read_cases(ocr_items("cs_dj_adj_no_header"))[0]]
    db.add(UserCourt(user_id=user.id, state_code="26", state_name="Delhi", dist_code="6", dist_name="South West",
                     complex_value="1260006@1,2@Y", complex_name="Dwarka"))
    await db.commit()
    job = await run_screenshot(db, user, session_factory, sync_redis, reads, monkeypatch, telegram_chat_id="555",
                               courts=[{"state_code": "26", "dist_code": "6", "complex_value": "1260006@1,2@Y", "name": "Dwarka"}])
    chat, text, markup = sent[0]
    assert chat == "555" and "Found 2 cases" in text
    assert_valid_markdown_v2(text)
    data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]
    assert f"fa:{job.id}:all" in data and len(data) == 3


async def test_bot_photo_starts_a_search(db, user, redis, monkeypatch):
    import app.redis as app_redis

    user.telegram_chat_id = "555"
    await db.commit()
    monkeypatch.setattr(app_redis, "get_redis", lambda: redis)
    started = []
    monkeypatch.setattr(jobs, "start_job", lambda job_id: started.append(job_id))
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"img")))
    photo = SimpleNamespace(file_size=1000, get_file=AsyncMock(return_value=file))
    update, ctx = make_update()
    update.effective_message.photo = [photo]
    update.effective_message.document = None
    await tb.photo_received(update, ctx)
    assert started and "Reading the case details" in replies(update)[0]
    job = await db.get(SearchJob, started[0])
    assert job.kind == "screenshot" and job.params["telegram_chat_id"] == "555"
    assert await redis.get(f"img:{job.params['images'][0]}")


async def test_add_button_tracks_the_case(db, user, monkeypatch):
    import search.add

    user.telegram_chat_id = "555"
    job = SearchJob(user_id=user.id, kind="screenshot", params={"read": []}, status="done", total=1, done=1, failed=0)
    db.add(job)
    await db.commit()
    hit = SearchHit(job_id=job.id, cnr_number="DLWT020142452016", case_number="Cr. Case/65303/2016",
                    petitioner="STATE", respondent="X", score=1.0, source="ecourts")
    db.add(hit)
    await db.commit()
    monkeypatch.setattr(search.add, "fetch_details", lambda ids: None)
    update, ctx = make_update()
    update.callback_query = SimpleNamespace(data=f"fa:{job.id}:{hit.id}", answer=AsyncMock())
    await tb.add_from_screenshot(update, ctx)
    assert "Added 1 case" in replies(update)[0]
    assert await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id)) is not None


def test_help_mentions_screenshots():
    assert "screenshot" in tb.HELP_TEXT
    assert_valid_markdown_v2(tb.HELP_TEXT)


# --- Web ---

async def test_upload_screenshots_on_the_website(client, user, redis, db, monkeypatch):
    client.cookies.set(SESSION_COOKIE, create_web_token(user.id))
    client.cookies.set(CSRF_COOKIE, CSRF)
    started = []
    monkeypatch.setattr(jobs, "start_job", lambda job_id: started.append(job_id))
    assert "Choose screenshots" in (await client.get("/cases/new?by=screenshot")).text
    r = await client.post("/find/screenshot", data={"csrf_token": CSRF},
                          files=[("images", ("a.png", b"\x89PNG...", "image/png")), ("images", ("b.jpg", b"jpg", "image/jpeg"))])
    assert r.status_code == 303 and started
    job = await db.get(SearchJob, started[0])
    assert job.kind == "screenshot" and len(job.params["images"]) == 2
    r = await client.post("/find/screenshot", data={"csrf_token": CSRF}, files=[("images", ("x.pdf", b"%PDF", "application/pdf"))])
    assert "isn&#39;t an image" in r.text
