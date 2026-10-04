"""Order PDFs and their text, the judge's name and cause lists for district cases."""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth.jwt import create_web_token
from app.web.session import CSRF_COOKIE, SESSION_COOKIE
from config.timeutils import today_ist
from models.database import CauseListing, CourtCase, CourtType, OrderDocument, TrackedCase
from scraper import extras
from scraper.district import (
    complex_for_est, judge_from_court_option, parse_cause_list, parse_court_ref, parse_orders, stored_orders,
)

CNR = "DLWT020000012016"

# The shapes of the district portal's markup (made-up case and people)
CASE_PAGE = """
<table class="history_table table " class="history_table"><tbody>
<tr><td align='left'>JUDICIAL MAGISTRATE FIRST CLASS - 11</td><td align='left'><a href='#'
 onclick=viewBusiness('2','9','20261027','DLWT020000012016','26','Pending','14-08-2026','802','DLWT02','cnr','56')>14-08-2026</a></td>
 <td>27-10-2026</td><td> Arguments</td></tr>
<tr><td align='left'>JUDICIAL MAGISTRATE FIRST CLASS - 9</td><td align='left'><a href='#'
 onclick=viewBusiness('2','9','20260814','DLWT020000012016','26','Pending','05-06-2026','790','DLWT02','cnr','55')>05-06-2026</a></td>
 <td>14-08-2026</td><td> Evidence</td></tr></tbody></table>
<table width="100%" class="order_table table " align="center" border="1"><thead><tr><th>Order Number</th>
<th>Order Date</th><th>Order Details</th></tr></thead><tr><td>&nbsp;&nbsp;1</td>
<td style='border-top:none;'>&nbsp;&nbsp;07-10-2016</font></td>
<td colspan='3'><a href='#' aria-label= 'Interim Orders | Order on Exhibit on 07-10-2016 '
 onclick=displayPdf('a1','b1','c1','d1','') ><font> COPY OF ORDER </font></a> </td></tr><tr><td>&nbsp;&nbsp;2</td>
<td style='border-top:none;'>&nbsp;&nbsp;14-08-2026</font></td>
<td colspan='3'><a href='#' aria-label= 'Interim Orders | Order on Exhibit on 14-08-2026 '
 onclick=displayPdf('a2','b2','c2','d2','') ><font> COPY OF ORDER </font></a> </td></tr></table>
"""

CAUSE_LIST = """<div id='table_heading'><center><span>Chief Metropolitan Magistrate, West, THC</span><br/>
<span>In the court of&nbsp;:&nbsp;Asha Rani</span><br/></center>Criminal Cases Listed on&nbsp;05-10-2026<br/>
<span>VC url : https://example.webex.com/meet/jmfc11</span></center></div>
<table id='dispTable'><thead><tr><th>Sr No</th><th>Cases</th><th>Party Name</th><th>Advocate Name</th></tr></thead>
<tbody><tr><th colspan=3 id='case_type_lable'>Urgent Cases</th></tr><tr><td colspan='6'>Misc./ Appearance</td></tr>
<tr><td>1</td><td><a class='someclass' href='#' onClick="viewHistory('2024','DLWT020000992011',2,'','CLcauselist',26,9,1260010,'CauseList')">View</a>Ct. Cases/4768/2016<br/><br></td>
<td>RAM LAL<br/>versus<br/>SITA DEVI</td><td><br/><br/></td></tr>
<tr><td colspan='6'>Arguments</td></tr>
<tr><td>2</td><td><a class='someclass' href='#' onClick="viewHistory('2021','DLWT020000012016',2,'','CLcauselist',26,9,1260010,'CauseList')">View</a>Cr. Case/71234/2016<br/><br></td>
<td>STATE<br/>versus<br/>MOHAN LAL</td><td>R. K. GUPTA<br/></td></tr></tbody></table>"""


def test_reads_orders_court_and_judge():
    orders = parse_orders(CASE_PAGE)
    assert [(o["number"], o["date"], o["description"]) for o in orders] == [
        ("1", "2016-10-07", "Interim Order"), ("2", "2026-08-14", "Interim Order")]
    assert orders[1]["pdf_args"] == ["a2", "b2", "c2", "d2", ""]
    assert "pdf_args" not in stored_orders(CASE_PAGE)[0]  # session-bound: never stored
    # The latest history row is the court hearing it now
    assert parse_court_ref(CASE_PAGE) == {"state_code": "26", "dist_code": "9", "est_code": "2", "court_no": "802"}
    assert judge_from_court_option("802-Asha Rani-JUDICIAL MAGISTRATE FIRST CLASS - 11") == "Asha Rani"
    assert judge_from_court_option("801-SH. VIJAY KUMAR-ADDITIONAL SESSIONS JUDGE") == "SH. VIJAY KUMAR"
    assert judge_from_court_option("17-JUDICIAL MAGISTRATE-JMFC") == ""  # no name listed
    assert complex_for_est({"1260001@1,2@Y": "A", "1260010@3,4@Y": "B"}, "4") == "1260010@3,4@Y"


def test_reads_a_cause_list():
    cl = parse_cause_list(CAUSE_LIST)
    assert (cl["judge"], cl["vc_url"]) == ("Asha Rani", "https://example.webex.com/meet/jmfc11")
    assert cl["entries"][1] == {"serial": 2, "cnr": "DLWT020000012016", "case": "Cr. Case/71234/2016",
                                "parties": "STATE vs MOHAN LAL", "advocates": "R. K. GUPTA",
                                "purpose": "Arguments", "category": "Urgent Cases"}


class FakePortal:
    """The district portal, one session per `async with`."""
    calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def case_page(self, cnr):
        self.calls.append(("page", cnr))
        return CASE_PAGE

    async def order_pdf(self, page, number):
        self.calls.append(("pdf", number))
        return b"%PDF-1.4 order " + number.encode()

    async def complexes(self, state, dist):
        return {"1260010@1,2,3,4@Y": "Tis Hazari Court Complex"}

    async def establishments(self, state, dist, complex_value):
        return {"1": "District and Sessions Judge, West, THC", "2": "Chief Metropolitan Magistrate, West, THC"}

    async def court_options(self, state, dist, complex_value, est):
        return {"2^802": "802-Asha Rani-JUDICIAL MAGISTRATE FIRST CLASS - 11"}

    async def cause_list(self, ref, complex_value, option, on, criminal):
        self.calls.append(("cause_list", on, criminal))
        return CAUSE_LIST if criminal else ""


@pytest.fixture(autouse=True)
def fake_portal(monkeypatch):
    FakePortal.calls = []
    monkeypatch.setattr(extras, "DistrictPortal", FakePortal)
    monkeypatch.setattr(extras, "pdf_text", lambda pdf: "Present: Ld. APP for the State.\nAccused present.\nPut up on 27.10.2026.")
    return FakePortal


@pytest_asyncio.fixture
async def case(db, user):
    c = CourtCase(cnr_number=CNR, case_type="Cr. Case", case_number="71234/2016", court_type=CourtType.DISTRICT,
                  petitioner="STATE", respondent="MOHAN LAL", judge="802-JUDICIAL MAGISTRATE FIRST CLASS - 11",
                  court_ref={"state_code": "26", "dist_code": "9", "est_code": "2", "court_no": "802"},
                  orders_json=stored_orders(CASE_PAGE), next_hearing_date=today_ist() + timedelta(days=1),
                  data_hash="x")
    db.add(c)
    await db.commit()
    db.add(TrackedCase(user_id=user.id, case_id=c.id))
    await db.commit()
    return c


async def test_enrich_finds_judge_and_reads_latest_order(db, case, sync_redis, fake_portal):
    await extras.enrich_case(db, case.id, sync_redis)
    await db.refresh(case)
    assert case.judge_name == "Asha Rani"
    assert case.court_name == "Chief Metropolitan Magistrate, West, THC"  # the portal's case page leaves it out
    assert case.latest_order_text.startswith("Present: Ld. APP")
    doc = await db.scalar(select(OrderDocument).where(OrderDocument.case_id == case.id))
    assert doc.number == "2" and doc.pdf.startswith(b"%PDF")
    # Nothing new: no more eCourts calls
    fake_portal.calls.clear()
    await extras.enrich_case(db, case.id, sync_redis)
    assert not [c for c in fake_portal.calls if c[0] in ("page", "pdf")]


async def test_cause_list_finds_item_number(db, case, sync_redis, fake_portal):
    found = await extras.refresh_cause_lists(db, sync_redis, on=today_ist())
    assert found == 1
    listing = await db.scalar(select(CauseListing).where(CauseListing.case_id == case.id))
    assert (listing.serial, listing.purpose, listing.vc_url) == (2, "Arguments", "https://example.webex.com/meet/jmfc11")
    assert listing.listing_date == case.next_hearing_date
    # Criminal list first for a Cr. Case; it had the case, so the civil list wasn't fetched
    assert [c[2] for c in fake_portal.calls if c[0] == "cause_list"] == [True]


async def test_unpublished_cause_list_is_retried_later(db, case, sync_redis, monkeypatch):
    async def empty(self, *a, **k):
        return ""

    monkeypatch.setattr(FakePortal, "cause_list", empty)
    assert await extras.refresh_cause_lists(db, sync_redis, on=today_ist()) == 0
    assert await db.scalar(select(CauseListing)) is None


@pytest_asyncio.fixture
async def web(client, user):
    client.cookies.set(SESSION_COOKIE, create_web_token(user.id))
    client.cookies.set(CSRF_COOKIE, "test-csrf-token")
    return client


async def test_case_page_shows_latest_order_judge_and_cause_list(web, db, case, sync_redis, monkeypatch):
    import app.web.router as web_router

    monkeypatch.setattr(extras, "request_cause_list", lambda case_id: None)
    await extras.enrich_case(db, case.id, sync_redis)
    await extras.refresh_cause_lists(db, sync_redis, on=today_ist())
    r = await web.get(f"/cases/{case.id}/view")
    assert r.status_code == 200
    page = r.text
    assert page.index("Latest order") < page.index("Next hearing")  # latest order on top
    assert "Present: Ld. APP for the State." in page
    assert "Before Asha Rani" in page
    assert "item 2" in page and "Join VC" in page
    # Every district order opens through us
    assert f"/cases/{case.id}/orders/1.pdf" in page and f"/cases/{case.id}/orders/2.pdf" in page


async def test_order_pdf_is_fetched_once_then_served_from_store(web, db, case, fake_portal):
    r = await web.get(f"/cases/{case.id}/orders/1.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    fake_portal.calls.clear()
    r = await web.get(f"/cases/{case.id}/orders/1.pdf")
    assert r.status_code == 200 and fake_portal.calls == []
    # Unknown order: back to the case with a message
    r = await web.get(f"/cases/{case.id}/orders/9.pdf")
    assert r.status_code == 303


async def test_order_pdf_needs_the_case_in_your_list(web, db, case, make_user):
    other = await make_user(phone="+919800000002")
    from models.database import TrackedCase as TC

    t = await db.scalar(select(TC).where(TC.case_id == case.id))
    t.user_id = other.id
    await db.commit()
    r = await web.get(f"/cases/{case.id}/orders/1.pdf")
    assert r.status_code == 404
