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
def no_background_fetch(monkeypatch):
    import search.add

    monkeypatch.setattr(search.add, "fetch_details", lambda ids: None)


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

# Full eCourts records the screenshot matches are checked against (hearing dates match the fixtures)
RECORDS = {
    "DLWT020142452016": {"case_number": "65303/2016", "case_type": "Cr. Case", "next_hearing_date": "2026-10-27",
                         "petitioner": "STATE", "respondent": "NARESH PAL SHARMA", "stage": "Arguments",
                         "court_name": "Chief Metropolitan Magistrate, West, THC",
                         "raw_data": {"history": [{"hearing_date": "2026-08-14", "business_date": "2026-06-05"}]}},
    "DLSW010079232021": {"case_number": "812/2021", "case_type": "CS DJ ADJ", "next_hearing_date": "2026-02-21",
                         "petitioner": "RAHUL VERMA", "respondent": "MAHESH SINGH GILL", "stage": "Evidence",
                         "raw_data": {"history": []}},
}


class Engine:
    """eCourts as seen by search.resolve: two Delhi districts, one split complex."""

    def __init__(self, records=None):
        self.searched = []
        self.records = RECORDS if records is None else records
        self.checked = []

    async def case_details(self, cnr):
        self.checked.append(cnr)
        if cnr not in self.records:
            raise LookupError("not found")
        return self.records[cnr]

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

async def run_screenshot(db, user, session_factory, sync_redis, reads, monkeypatch, engine=None, **params):
    monkeypatch.setattr(screenshot, "read_image", lambda data: reads)
    key = jobs.store_image(sync_redis, b"fake image bytes")
    job = await jobs.create_job(db, user, "screenshot", {"images": [key], "courts": [], **params})
    await jobs.run_job(job.id, session_factory, sync_redis, engine=engine or Engine())
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
    assert chat == "555" and "Added 2 cases" in text  # both agreed with their eCourts records: added without asking
    assert "Next: 27 Oct 2026" in text.replace("\\", "")
    assert_valid_markdown_v2(text)
    # Nothing left to confirm: no Add buttons, just a Remove (undo) per added case
    callbacks = [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row if "callback_data" in b]
    assert len(callbacks) == 2 and all(c.startswith(f"rm:{job.id}:") for c in callbacks)
    assert len((await db.scalars(select(TrackedCase).where(TrackedCase.user_id == user.id))).all()) == 2


async def test_unsure_match_waits_for_a_tap(db, user, session_factory, sync_redis, monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="MarkdownV2", reply_markup=None):
        sent.append((text, reply_markup))

    import search.telegram_reply as tr

    monkeypatch.setattr(tr, "send_telegram_message", fake_send)
    # Right court and number, but only one of the screenshot's parties matches
    read = ReadCase(court_header="Chief Metropolitan Magistrate, West, THC,West", case_type="Cr. Case",
                    number="65303", year="2016", petitioner="NARESH PAL SHARMA", respondent="OM PRAKASH")
    db.add(UserCourt(user_id=user.id, state_code="26", state_name="Delhi", dist_code="8", dist_name="West",
                     complex_value="1260001@1,2@Y", complex_name="Tis Hazari"))
    await db.commit()
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch, telegram_chat_id="555",
                               courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "Tis Hazari"}])
    text, markup = sent[0]
    assert "Please check" in text and not job.params.get("added")
    assert_valid_markdown_v2(text)
    assert markup["inline_keyboard"][0][0]["callback_data"].startswith(f"fa:{job.id}:")


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
    assert started and "added to your list automatically" in replies(update)[0]
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


# --- Telling the right case from look-alikes ---

async def test_sibling_case_with_other_hearing_dates_is_not_added(db, user, session_factory, sync_redis, monkeypatch):
    """A misread digit lands on another case of the same parties: its hearing dates give it away."""
    sent = []

    async def fake_send(chat_id, text, parse_mode="MarkdownV2", reply_markup=None):
        sent.append(text)

    import search.telegram_reply as tr

    monkeypatch.setattr(tr, "send_telegram_message", fake_send)
    sibling = dict(RECORDS["DLWT020142452016"], next_hearing_date="2026-11-30",
                   raw_data={"history": [{"hearing_date": "2026-07-01"}]})
    read = read_cases(ocr_items("tis_hazari_cr_case"))[0]  # screenshot says Aug 14 2026
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch, telegram_chat_id="555",
                               engine=Engine(records={"DLWT020142452016": sibling}),
                               courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "Tis Hazari"}])
    assert not job.params.get("added")
    assert "Hearing date differs" in sent[0]
    assert await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id)) is None


class TwoCourts(Engine):
    """The same CS DJ ADJ number exists at Tis Hazari and Dwarka."""

    async def case_number(self, court, code, number, year):
        self.searched.append((court.complex_code, code, number, year))
        if code == "5^1":
            cnr = "DLWT010000122021" if court.complex_code == "1260001" else "DLSW010079232021"
            return [Hit(cnr, f"CS DJ ADJ/{number}/{year}", "CS DJ ADJ", year, "RAHUL VERMA", "MAHESH SINGH GILL")]
        return []


async def test_same_number_in_two_courts_asks_instead_of_guessing(db, user, session_factory, sync_redis, monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="MarkdownV2", reply_markup=None):
        sent.append(text)

    import search.telegram_reply as tr

    monkeypatch.setattr(tr, "send_telegram_message", fake_send)
    records = dict(RECORDS, DLWT010000122021=dict(RECORDS["DLSW010079232021"], next_hearing_date="2026-05-02"))
    read = read_cases(ocr_items("cs_dj_adj_no_header"))[0]
    for dist, name in (("8", "West"), ("6", "South West")):
        db.add(UserCourt(user_id=user.id, state_code="26", state_name="Delhi", dist_code=dist, dist_name=name,
                         complex_value="1260001@1,2@Y" if dist == "8" else "1260006@1,2@Y", complex_name=name))
    await db.commit()
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch, telegram_chat_id="555",
                               engine=TwoCourts(records=records),
                               courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "West"},
                                       {"state_code": "26", "dist_code": "6", "complex_value": "1260006@1,2@Y", "name": "South West"}])
    hits = (await db.scalars(select(SearchHit).where(SearchHit.job_id == job.id))).all()
    assert len(hits) == 2  # both courts checked, not just the first
    # Only one of them agrees with the screenshot's date (Feb 21 2026), so that one is added
    assert job.params.get("added") == ["DLSW010079232021"]


async def test_same_number_and_nothing_to_tell_them_apart(db, user, session_factory, sync_redis, monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="MarkdownV2", reply_markup=None):
        sent.append(text)

    import search.telegram_reply as tr

    monkeypatch.setattr(tr, "send_telegram_message", fake_send)
    read = ReadCase(case_type="CS DJ ADJ", number="812", year="2021", petitioner="RAHUL VERMA",
                    respondent="MAHESH SINGH GILL")  # no court, no date
    records = dict(RECORDS, DLWT010000122021=RECORDS["DLSW010079232021"])
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch, telegram_chat_id="555",
                               engine=TwoCourts(records=records),
                               courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "West"},
                                       {"state_code": "26", "dist_code": "6", "complex_value": "1260006@1,2@Y", "name": "South West"}])
    assert not job.params.get("added")
    assert "More than one case has this number" in sent[0].replace("\\", "")


async def test_state_case_needs_more_than_the_parties(db, user, session_factory, sync_redis, monkeypatch):
    """"STATE vs X" with no date on the screenshot: nothing independent confirms it."""
    read = ReadCase(court_header="Chief Metropolitan Magistrate, West, THC,West", case_type="Cr. Case",
                    number="65303", year="2016", petitioner="STATE", respondent="")
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch, courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "Tis Hazari"}])
    assert job.params["read"][0]["found"]  # found, just not sure
    assert not job.params.get("added")


async def test_cnr_whose_record_disagrees_is_not_added(db, user, session_factory, sync_redis, monkeypatch):
    """A misread CNR points at a real but different case: its number gives it away."""
    read = ReadCase(cnr="DLWT020142452016", case_type="Cr. Case", number="71234", year="2016", next_date="27-10-2026")
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch)
    assert not job.params.get("added")
    read = ReadCase(cnr="DLWT020142452016", case_type="Cr. Case", number="65303", year="2016", next_date="27-10-2026")
    job = await run_screenshot(db, user, session_factory, sync_redis, [read], monkeypatch)
    assert job.params.get("added") == ["DLWT020142452016"]


def test_case_history_screen_uses_registration_not_filing_number():
    # OCR boxes as the eCourts app's Case History screen gives them (numbers changed)
    items = [(130, 120, "Case History"), (230, 247, "Case Details"),
             (263, 350, "Cr. Case/130001/2016"), (40, 366, "Filing Number"), (263, 443, "23-09-2016"),
             (40, 459, "Filing Date"), (40, 535, "Registration"), (263, 536, "Cr. Case/71234/2016"),
             (40, 570, "Number"), (40, 628, "Registration"), (263, 629, "23-09-2016"), (40, 662, "Date"),
             (263, 722, "DLWT020000012016"), (40, 738, "CNR Number"), (230, 839, "Case Status"),
             (40, 941, "First Hearing"), (263, 942, "07-10-2016"), (40, 975, "Date"), (40, 1034, "Next Hearing"),
             (263, 1035, "27-10-2026"), (40, 1068, "Date"), (263, 1128, "Arguments"), (40, 1144, "Case Stage")]
    [c] = read_cases(items)
    assert (c.cnr, c.case_type, c.number, c.year, c.next_date) == ("DLWT020000012016", "Cr. Case", "71234", "2016", "27-10-2026")
    # Same screen with the CNR cut off: still the registration number
    [c] = read_cases([i for i in items if not i[2].startswith("DLWT")])
    assert (c.cnr, c.number) == ("", "71234")


async def test_remove_button_undoes_an_automatic_add(db, user, session_factory, sync_redis, monkeypatch):
    user.telegram_chat_id = "555"
    await db.commit()
    job = await run_screenshot(db, user, session_factory, sync_redis, [read_cases(ocr_items("tis_hazari_cr_case"))[0]],
                               monkeypatch, courts=[{"state_code": "26", "dist_code": "8", "complex_value": "1260001@1,2@Y", "name": "Tis Hazari"}])
    [case_id] = job.params["added_case_ids"]
    update, ctx = make_update()
    update.callback_query = SimpleNamespace(data=f"rm:{job.id}:{case_id}", answer=AsyncMock())
    await tb.remove_added_case(update, ctx)
    assert "Removed" in replies(update)[0]
    assert await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id)) is None
    # Can't be used on cases the job didn't add
    update, ctx = make_update()
    update.callback_query = SimpleNamespace(data=f"rm:{job.id}:999999", answer=AsyncMock())
    await tb.remove_added_case(update, ctx)
    assert replies(update) == []
