import pytest
from sqlalchemy import select

from config.settings import settings
from models.database import TrackedCase
from tests.conftest import CNR_A, CNR_B


async def test_get_and_update_profile(client, auth, make_user):
    r = await client.get("/users/me", headers=auth)
    assert r.json()["telegram_linked"] is False and r.json()["notification_time"] == "08:00"

    r = await client.put(
        "/users/me",
        json={"name": "Adv. R. Iyer", "email": "iyer@example.com", "bar_registration_no": "D/1234/2010", "whatsapp_number": "98765 00000"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["email"] == "iyer@example.com" and r.json()["whatsapp_number"] == "+919876500000"

    await make_user(phone="+916666666666", email="taken@example.com")
    r = await client.put("/users/me", json={"email": "taken@example.com"}, headers=auth)
    assert r.status_code == 409

    r = await client.put("/users/me", json={"notification_time": "25:00"}, headers=auth)
    assert r.status_code == 422


async def test_notification_prefs_apply_to_all_cases(client, auth, db):
    for cnr in (CNR_A, CNR_B):
        await client.post("/cases/track", json={"cnr_number": cnr}, headers=auth)
    r = await client.put(
        "/users/me/notifications",
        json={"notification_time": "07:30", "digest_day": 6, "channels": {"email": False}},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["notification_time"] == "07:30" and r.json()["digest_day"] == 6
    rows = (await db.scalars(select(TrackedCase))).all()
    assert [t.notify_email for t in rows] == [False, False]
    assert [t.notify_telegram for t in rows] == [True, True]

    r = await client.put("/users/me/notifications", json={"channels": {"whatsapp": True}}, headers=auth)
    assert r.status_code == 402


@pytest.fixture
def self_upgrade(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PLAN_SELF_UPGRADE", True)


async def test_plan_upgrade_needs_payment_by_default(client, auth):
    r = await client.post("/users/me/plan/upgrade", json={"plan": "firm", "whatsapp_addon": True}, headers=auth)
    assert r.status_code == 402
    assert (await client.get("/users/me/plan", headers=auth)).json()["plan"] == "free"


async def test_null_preferences_are_ignored(client, auth):
    r = await client.put("/users/me", json={"notification_time": None, "digest_day": None, "name": None}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["notification_time"] == "08:00" and body["digest_day"] == 0 and body["name"] == "Adv. Test"


async def test_plan_upgrade_and_whatsapp_addon(client, auth, db, self_upgrade):
    await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)

    plan = (await client.get("/users/me/plan", headers=auth)).json()
    assert plan["plan"] == "free" and plan["cases_tracked"] == 1 and plan["max_cases"] == 5 and plan["price_monthly"] == 0

    r = await client.post("/users/me/plan/upgrade", json={"plan": "pro", "whatsapp_addon": True}, headers=auth)
    assert r.status_code == 200
    plan = r.json()
    assert plan["plan"] == "pro" and plan["max_cases"] == 100 and plan["whatsapp_addon"] is True
    assert plan["price_monthly"] == 499 + 199 and plan["plan_expires_at"]

    tracked = await db.scalar(select(TrackedCase))
    assert tracked.notify_whatsapp is True

    # New cases get WhatsApp on by default once the add-on is active
    r = await client.post("/cases/track", json={"cnr_number": CNR_B}, headers=auth)
    assert r.json()["notify_whatsapp"] is True


async def test_downgrade_blocked_when_over_limit(client, auth, scraper, self_upgrade):
    from tests.conftest import make_detail

    await client.post("/users/me/plan/upgrade", json={"plan": "starter"}, headers=auth)
    for i in range(6):
        cnr = f"DLWE01000{i}002024"
        scraper.details[cnr] = make_detail(cnr)
        assert (await client.post("/cases/track", json={"cnr_number": cnr}, headers=auth)).status_code == 201
    r = await client.post("/users/me/plan/upgrade", json={"plan": "free"}, headers=auth)
    assert r.status_code == 409
