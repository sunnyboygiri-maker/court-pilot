from datetime import timedelta

from sqlalchemy import func, select

from app.auth.jwt import create_access_token
from config.timeutils import today_ist
from models.database import CaseStatus, CourtCase, CourtType, PlanTier, TrackedCase
from tests.conftest import CNR_A, CNR_B, CNR_HC, make_detail


async def test_track_fetches_from_ecourts_and_maps_fields(client, auth, scraper, db):
    r = await client.post("/cases/track", json={"cnr_number": "dlwe-0100-1234-2024", "label": "Kumar bail"}, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    case = body["case"]
    assert body["label"] == "Kumar bail"
    assert case["cnr_number"] == CNR_A
    assert case["title"] == "Ramesh Kumar & Ors. vs State of NCT of Delhi"
    assert case["status"] == "pending"
    assert case["court_type"] == "district"
    assert case["judge"] == "Addl. Sessions Judge-02"
    assert case["next_hearing_date"] == (today_ist() + timedelta(days=5)).isoformat()
    assert case["latest_order_link"] == "https://ecourts.example/o0.pdf"
    assert case["view_url"] == f"https://courtpilot.test/case/{CNR_A}/view"
    assert scraper.calls == [CNR_A]

    row = await db.scalar(select(CourtCase).where(CourtCase.cnr_number == CNR_A))
    assert row.data_hash and row.last_polled_at is not None
    assert row.petitioner_advocate == "A. Sharma, A. Sharma"
    assert row.acts_sections == [{"act": "Indian Penal Code", "section": "420"}]
    assert row.filing_year == 2024


async def test_high_court_cnr_maps_court_type(client, auth):
    r = await client.post("/cases/track", json={"cnr_number": CNR_HC}, headers=auth)
    assert r.status_code == 201
    assert r.json()["case"]["court_type"] == CourtType.HIGH_COURT.value


async def test_shared_case_is_fetched_once(client, auth, make_user, scraper, db):
    other = await make_user(phone="+919999999999")
    other_auth = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    assert (await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)).status_code == 201
    assert (await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=other_auth)).status_code == 201
    assert scraper.calls == [CNR_A]
    assert await db.scalar(select(func.count()).select_from(CourtCase)) == 1
    assert await db.scalar(select(func.count()).select_from(TrackedCase)) == 2


async def test_track_errors(client, auth, scraper):
    assert (await client.post("/cases/track", json={"cnr_number": "NOTAVALIDCNR1234X"}, headers=auth)).status_code == 422
    r = await client.post("/cases/track", json={"cnr_number": "KAMY010000012020"}, headers=auth)
    assert r.status_code == 404

    scraper.fail.add(CNR_B)
    r = await client.post("/cases/track", json={"cnr_number": CNR_B}, headers=auth)
    assert r.status_code == 502

    assert (await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)).status_code == 201
    assert (await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)).status_code == 409


async def test_plan_limit_checked_before_scraping(client, auth, scraper):
    for i in range(5):
        cnr = f"DLWE01000{i}002024"
        scraper.details[cnr] = make_detail(cnr)
        assert (await client.post("/cases/track", json={"cnr_number": cnr}, headers=auth)).status_code == 201
    calls = len(scraper.calls)
    r = await client.post("/cases/track", json={"cnr_number": CNR_B}, headers=auth)
    assert r.status_code == 402
    assert "5 cases" in r.json()["detail"]
    assert len(scraper.calls) == calls


async def test_expired_paid_plan_falls_back_to_free_limit(client, make_user, scraper):
    from config.timeutils import utcnow

    lapsed = await make_user(phone="+918888888888", plan=PlanTier.STARTER, plan_expires_at=utcnow() - timedelta(days=1))
    headers = {"Authorization": f"Bearer {create_access_token(lapsed.id)}"}
    plan = (await client.get("/users/me/plan", headers=headers)).json()
    assert plan["effective_plan"] == "free" and plan["max_cases"] == 5


async def test_list_filters_and_upcoming(client, auth, scraper):
    for cnr in (CNR_A, CNR_B):
        await client.post("/cases/track", json={"cnr_number": cnr, "client_name": f"client-{cnr[:2]}"}, headers=auth)

    r = await client.get("/cases/", headers=auth)
    assert [c["case"]["cnr_number"] for c in r.json()] == [CNR_A, CNR_B]  # soonest hearing first

    r = await client.get("/cases/upcoming", headers=auth)
    assert [c["case"]["cnr_number"] for c in r.json()] == [CNR_A]

    r = await client.get("/cases/", params={"q": "client-MH"}, headers=auth)
    assert [c["case"]["cnr_number"] for c in r.json()] == [CNR_B]

    to = (today_ist() + timedelta(days=10)).isoformat()
    r = await client.get("/cases/", params={"hearing_to": to}, headers=auth)
    assert [c["case"]["cnr_number"] for c in r.json()] == [CNR_A]

    r = await client.get("/cases/", params={"status": "disposed"}, headers=auth)
    assert r.json() == []


async def test_get_update_untrack(client, auth, make_user):
    r = await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)
    case_id = r.json()["case"]["id"]

    r = await client.get(f"/cases/{case_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["case"]["orders"][0]["link"] == "https://ecourts.example/o0.pdf"
    assert r.json()["snapshots"] == []

    r = await client.put(f"/cases/{case_id}", json={"label": "Bail matter", "priority": 2, "notes": "Carry file"}, headers=auth)
    assert r.status_code == 200 and r.json()["label"] == "Bail matter" and r.json()["priority"] == 2

    # WhatsApp needs the add-on
    r = await client.put(f"/cases/{case_id}", json={"notify_whatsapp": True}, headers=auth)
    assert r.status_code == 402

    # Other users can't see or change it
    other = await make_user(phone="+917777777777")
    other_auth = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    assert (await client.get(f"/cases/{case_id}", headers=other_auth)).status_code == 404
    assert (await client.delete(f"/cases/{case_id}/untrack", headers=other_auth)).status_code == 404

    assert (await client.delete(f"/cases/{case_id}/untrack", headers=auth)).status_code == 204
    assert (await client.get(f"/cases/{case_id}", headers=auth)).status_code == 404


async def test_search_advocate(client, auth, scraper, monkeypatch):
    await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)

    async def fake_search(name, state, district=None, bar_code=None):
        assert state == "delhi" and name == "Sharma"
        return [
            {"cnr_number": CNR_A, "petitioner": "Ramesh Kumar", "status": "Pending"},
            {"cnr_number": CNR_HC, "petitioner": "X", "status": "Pending"},
        ]

    monkeypatch.setattr(scraper, "fetch_cases_by_advocate", fake_search)
    r = await client.post("/cases/search-advocate", json={"advocate_name": "Sharma", "state": "delhi"}, headers=auth)
    assert r.status_code == 200
    assert [(x["cnr_number"], x["already_tracked"]) for x in r.json()] == [(CNR_A, True), (CNR_HC, False)]

    r = await client.post("/cases/search-advocate", json={"state": "delhi"}, headers=auth)
    assert r.status_code == 422


def test_resolve_high_court():
    from scraper.ecourts import resolve_high_court

    assert resolve_high_court("delhi").code == "delhi"
    assert resolve_high_court("Maharashtra").code == "bombay"
    assert resolve_high_court("Tamil Nadu").code == "madras"
    assert resolve_high_court("Karnataka").code == "karnataka"
    assert resolve_high_court("Jammu & Kashmir").code == "jammu"
    assert resolve_high_court("Atlantis") is None
