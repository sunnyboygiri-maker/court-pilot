"""Web app: login, dashboard, add/edit/untrack cases, settings, CSRF and session rules."""
import re
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth import otp
from app.auth.jwt import create_access_token, create_web_token
from app.web.session import CSRF_COOKIE, SESSION_COOKIE
from config.settings import settings
from config.timeutils import today_ist
from models.database import CourtCase, TrackedCase, User
from tests.conftest import CNR_A, CNR_B, CNR_HC

CSRF = "test-csrf-token"


def form(**fields) -> dict:
    return {"csrf_token": CSRF, **fields}


@pytest.fixture
def sent_codes(monkeypatch):
    sent = {}

    async def fake_deliver(phone, code, telegram_chat_id=None):
        sent[phone] = code

    monkeypatch.setattr(otp, "deliver_otp", fake_deliver)
    return sent


@pytest_asyncio.fixture
async def web(client, user):
    """A browser that is logged in as `user`."""
    client.cookies.set(SESSION_COOKIE, create_web_token(user.id))
    client.cookies.set(CSRF_COOKIE, CSRF)
    return client


async def track(web, cnr, **extra):
    r = await web.post("/cases/new", data=form(cnr=cnr, **extra))
    assert r.status_code == 303, r.text
    return int(re.search(r"/cases/(\d+)/view", r.headers["location"]).group(1))


# --- Access ---

async def test_pages_redirect_to_login(client):
    for path in ("/", "/cases", "/cases/new", "/settings", "/cases/1/view"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


async def test_htmx_requests_are_told_to_navigate_to_login(client):
    r = await client.get("/cases", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"] == "/login"


async def test_api_token_is_not_a_web_session_and_vice_versa(client, user):
    client.cookies.set(SESSION_COOKIE, create_access_token(user.id))
    assert (await client.get("/")).status_code == 303
    r = await client.get("/users/me", headers={"Authorization": f"Bearer {create_web_token(user.id)}"})
    assert r.status_code == 401


async def test_docs_hidden_outside_debug(client):
    assert not settings.DEBUG
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_api_routes_still_work_next_to_web_routes(client, auth, scraper):
    r = await client.post("/cases/track", json={"cnr_number": CNR_A}, headers=auth)
    case_id = r.json()["case"]["id"]
    assert (await client.get(f"/cases/{case_id}", headers=auth)).json()["case"]["cnr_number"] == CNR_A
    assert (await client.get("/cases/", headers=auth)).status_code == 200
    assert (await client.get("/cases/")).status_code == 401


# --- Login ---

async def test_login_flow_for_linked_user(client, make_user, sent_codes):
    await make_user(telegram_chat_id="555")
    r = await client.get("/login")
    assert r.status_code == 200 and "Mobile number" in r.text
    csrf = client.cookies[CSRF_COOKIE]

    r = await client.post("/login/send", data={"csrf_token": csrf, "phone": "98765 43210"})
    assert "Enter your code" in r.text and "Your name" not in r.text  # existing account
    code = sent_codes["+919876543210"]

    r = await client.post("/login/verify", data={"csrf_token": csrf, "phone": "+919876543210", "code": "000000" if code != "000000" else "111111"})
    assert "wrong or has expired" in r.text

    r = await client.post("/login/verify", data={"csrf_token": csrf, "phone": "+919876543210", "code": code})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)

    r = await client.get("/")
    assert r.status_code == 200 and "Hello, Adv. Test" in r.text and "logged in." in r.text


async def test_unlinked_number_is_shown_how_to_connect_telegram(client, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "CourtPilotBot")
    await client.get("/login")
    r = await client.post("/login/send", data={"csrf_token": client.cookies[CSRF_COOKIE], "phone": "9876543210"})
    assert "One more step for phone login" in r.text and "https://t.me/CourtPilotBot" in r.text


async def test_new_user_must_give_name(client, db, sent_codes, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)  # only way to get a code without a Telegram link
    await client.get("/login")
    csrf = client.cookies[CSRF_COOKIE]
    r = await client.post("/login/send", data={"csrf_token": csrf, "phone": "9876543210"})
    assert "Your name" in r.text
    code = sent_codes["+919876543210"]

    r = await client.post("/login/verify", data={"csrf_token": csrf, "phone": "+919876543210", "code": code})
    assert "Please tell us your name" in r.text  # code not used up
    r = await client.post("/login/verify", data={"csrf_token": csrf, "phone": "+919876543210", "code": code, "name": "Adv. Rao"})
    assert r.status_code == 303
    assert (await db.scalar(select(User).where(User.phone == "+919876543210"))).name == "Adv. Rao"


async def test_invalid_phone_message(client):
    await client.get("/login")
    r = await client.post("/login/send", data={"csrf_token": client.cookies[CSRF_COOKIE], "phone": "12345"})
    assert "10-digit mobile number" in r.text


async def test_post_without_csrf_token_is_rejected(web, db, user):
    r = await web.post("/settings/reminders", data={"time": "06:00", "day": "3"}, headers={"referer": "http://test/settings"})
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    await db.refresh(user)
    assert user.notification_time == "08:00"


async def test_logout(web):
    r = await web.post("/logout", data=form())
    assert r.status_code == 303 and r.headers["location"] == "/login"
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")]
    assert cleared and "Max-Age=0" in cleared[0]


# --- Dashboard & lists ---

async def test_empty_dashboard_explains_cnr(web):
    r = await web.get("/")
    assert "Add your first case" in r.text and "CNR number" in r.text
    assert "Reminders are off" in r.text  # Telegram not linked


async def test_dashboard_groups_hearings(web, db, scraper):
    ids = {cnr: await track(web, cnr) for cnr in (CNR_A, CNR_B, CNR_HC)}
    today = today_ist()
    for cnr, days in ((CNR_A, 0), (CNR_B, 1), (CNR_HC, 12)):
        c = await db.get(CourtCase, ids[cnr])
        c.next_hearing_date = today + timedelta(days=days)
    await db.commit()

    r = await web.get("/")
    text = r.text
    assert text.index('id="today"') < text.index('id="tomorrow"') < text.index('id="later"')
    assert 'id="week"' not in text  # empty groups are hidden
    assert "Ramesh Kumar &amp; Ors. vs State of NCT of Delhi" in text


async def test_cases_search_and_filter(web, scraper):
    await track(web, CNR_A, label="Mehta bail matter")
    await track(web, CNR_B, client_name="Gupta Traders")
    r = await web.get("/cases", params={"q": "gupta"})
    assert "1 case found" in r.text and "Gupta Traders" in r.text and "Mehta bail" not in r.text
    r = await web.get("/cases", params={"status": "disposed"})
    assert "No matching cases" in r.text


# --- Add, edit, untrack ---

async def test_add_case(web, db, user, scraper):
    case_id = await track(web, CNR_A.lower(), label="Fraud case", client_name="R. Kumar", notes="Bring FIR copy")
    tracked = await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id))
    assert tracked.case_id == case_id and tracked.label == "Fraud case" and tracked.notes == "Bring FIR copy"
    r = await web.get(f"/cases/{case_id}/view")
    assert r.status_code == 200 and "Case added" in r.text and "Fraud case" in r.text and "Open PDF" in r.text


@pytest.mark.parametrize("cnr,message", [
    ("ABC", "doesn&#39;t look like a CNR number"),
    ("DLWE019999992024", "eCourts has no case with this CNR number"),
])
async def test_add_case_errors_in_plain_language(web, cnr, message):
    r = await web.post("/cases/new", data=form(cnr=cnr, label="keep me"))
    assert r.status_code == 200 and message in r.text and 'value="keep me"' in r.text


async def test_add_case_when_ecourts_is_down(web, scraper):
    scraper.fail.add(CNR_A)
    r = await web.post("/cases/new", data=form(cnr=CNR_A))
    assert "eCourts isn&#39;t responding right now" in r.text


async def test_add_case_duplicate_and_plan_limit(web, scraper):
    from tests.conftest import make_detail

    case_id = await track(web, CNR_A)
    r = await web.post("/cases/new", data=form(cnr=CNR_A))
    assert "already tracking this case" in r.text and f"/cases/{case_id}/view" in r.text
    for i in range(4):
        cnr = f"DLWE01000{i}002024"
        scraper.details[cnr] = make_detail(cnr)
        await track(web, cnr)
    r = await web.post("/cases/new", data=form(cnr=CNR_B))
    assert "5 of 5 cases allowed on the Free plan" in r.text


async def test_edit_case(web, db, user, scraper):
    case_id = await track(web, CNR_A)
    r = await web.post(f"/cases/{case_id}/edit", data=form(label="New label", client_name="Client X", notes="Adjournment likely", priority="2"))
    assert r.status_code == 303
    tracked = await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id))
    await db.refresh(tracked)
    assert (tracked.label, tracked.client_name, tracked.notes, tracked.priority) == ("New label", "Client X", "Adjournment likely", 2)

    # htmx: the page comes back with an inline "Saved" instead of a redirect
    r = await web.post(f"/cases/{case_id}/edit", data=form(notes="Updated"), headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'class="saved"' in r.text


async def test_case_alert_toggles_and_whatsapp_lock(web, db, user, scraper):
    case_id = await track(web, CNR_A)
    await web.post(f"/cases/{case_id}/alerts", data=form(notify_telegram="on", notify_whatsapp="on"))
    tracked = await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id))
    await db.refresh(tracked)
    assert tracked.notify_telegram is True and tracked.notify_email is False
    assert tracked.notify_whatsapp is False  # no add-on


async def test_untrack(web, db, user, scraper):
    case_id = await track(web, CNR_A, label="Old matter")
    r = await web.post(f"/cases/{case_id}/untrack", data=form())
    assert r.status_code == 303 and r.headers["location"] == "/cases"
    assert await db.scalar(select(TrackedCase).where(TrackedCase.user_id == user.id)) is None
    assert "Stopped tracking Old matter" in (await web.get("/cases")).text


async def test_cannot_see_or_edit_someone_elses_case(web, client, make_user, scraper, db):
    other = await make_user(phone="+919999999999")
    client.cookies.set(SESSION_COOKIE, create_web_token(other.id))
    case_id = await track(client, CNR_A, notes="private")
    client.cookies.set(SESSION_COOKIE, create_web_token((await db.scalar(select(User).where(User.phone == "+919876543210"))).id))
    r = await client.get(f"/cases/{case_id}/view")
    assert r.status_code == 404 and "private" not in r.text
    r = await client.post(f"/cases/{case_id}/edit", data=form(notes="hacked"))
    tracked = await db.scalar(select(TrackedCase).where(TrackedCase.user_id == other.id))
    await db.refresh(tracked)
    assert tracked.notes == "private"


# --- Settings ---

async def test_settings_save(web, db, user, email_codes):
    r = await web.get("/settings")
    assert "0 of 5 cases" in r.text and "Coming soon" in r.text and "₹299" in r.text

    r = await web.post("/settings/reminders", data=form(time="07:30", day="6"))
    assert r.status_code == 303
    r = await web.post("/settings/profile", data=form(name="Adv. Meera Iyer", email="Meera@Example.com", bar="D/99/2010"))
    assert "Confirm your email" in r.text  # a new address isn't saved until confirmed
    await db.refresh(user)
    assert (user.notification_time, user.digest_day) == ("07:30", 6)
    assert (user.name, user.email, user.bar_registration_no) == ("Adv. Meera Iyer", None, "D/99/2010")

    code = email_codes["meera@example.com"]
    r = await web.post("/settings/email/verify", data=form(code="000000" if code != "000000" else "111111"))
    assert "wrong or has expired" in r.text
    r = await web.post("/settings/email/verify", data=form(code=code))
    assert r.status_code == 303
    await db.refresh(user)
    assert user.email == "meera@example.com" and user.email_verified is True
    assert "Confirmed" in (await web.get("/settings")).text


async def test_settings_validation(web, db, user, make_user):
    await make_user(phone="+919999999999", email="taken@example.com", email_verified=True)
    r = await web.post("/settings/profile", data=form(name="Adv. Test", email="taken@example.com"))
    assert "already used by another" in r.text
    r = await web.post("/settings/profile", data=form(name="", email=""))
    assert "Please enter your name" in r.text
    r = await web.post("/settings/reminders", data=form(time="25:00", day="1"))
    assert "Please choose a time" in r.text
    await db.refresh(user)
    assert user.notification_time == "08:00"


async def test_settings_offers_one_tap_telegram_link(web, redis, user, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "CourtPilotTestBot")
    r = await web.get("/settings")
    token = re.search(r"https://t.me/CourtPilotTestBot\?start=link_([\w-]+)", r.text).group(1)
    assert await redis.get(f"tg-link:{token}") == str(user.id)


# --- Email & Google login ---

@pytest.fixture
def email_codes(monkeypatch):
    sent = {}

    async def fake_deliver(email, code):
        sent[email] = code

    monkeypatch.setattr(otp, "deliver_email_code", fake_deliver)
    monkeypatch.setattr(otp, "email_configured", lambda: True)
    return sent


async def test_login_page_offers_google_email_and_phone(client, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(otp, "email_configured", lambda: True)
    r = await client.get("/login")
    assert "Continue with Google" in r.text and "Continue with email" in r.text and "+91" in r.text


async def test_login_page_hides_unconfigured_methods(client):
    r = await client.get("/login")
    assert "Continue with Google" not in r.text and "Continue with email" not in r.text


async def test_email_login_creates_account_then_logs_in(client, db, email_codes, redis):
    await client.get("/login?method=email")
    csrf = client.cookies[CSRF_COOKIE]
    r = await client.post("/login/email/send", data={"csrf_token": csrf, "email": "Priya@Example.com"})
    assert "Enter your code" in r.text and "Your name" in r.text
    code = email_codes["priya@example.com"]
    r = await client.post("/login/email/verify", data={"csrf_token": csrf, "email": "priya@example.com", "code": code, "name": "Adv. Priya"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = await db.scalar(select(User).where(User.email == "priya@example.com"))
    assert user.phone is None and user.email_verified and user.name == "Adv. Priya"
    assert "Hello, Adv. Priya" in (await client.get("/")).text
    assert "Not added" in (await client.get("/settings")).text  # no phone yet

    # Next time: same account, no name question
    client.cookies.delete(SESSION_COOKIE)
    await redis.delete("otp-cooldown:email:priya@example.com")
    r = await client.post("/login/email/send", data={"csrf_token": csrf, "email": "priya@example.com"})
    assert "Enter your code" in r.text and "Your name" not in r.text


async def test_email_login_ignores_unconfirmed_address_on_another_account(client, db, make_user, email_codes):
    squatter = await make_user(phone="+919111111111", email="victim@example.com")  # typed in, never confirmed
    await client.get("/login?method=email")
    csrf = client.cookies[CSRF_COOKIE]
    await client.post("/login/email/send", data={"csrf_token": csrf, "email": "victim@example.com"})
    r = await client.post("/login/email/verify", data={"csrf_token": csrf, "email": "victim@example.com",
                                                      "code": email_codes["victim@example.com"], "name": "Real Owner"})
    assert r.status_code == 303
    owner = await db.scalar(select(User).where(User.email == "victim@example.com"))
    assert owner.id != squatter.id and owner.email_verified
    await db.refresh(squatter)
    assert squatter.email is None


async def test_email_login_wrong_code(client, email_codes):
    await client.get("/login?method=email")
    csrf = client.cookies[CSRF_COOKIE]
    await client.post("/login/email/send", data={"csrf_token": csrf, "email": "a@example.com"})
    wrong = "000000" if email_codes["a@example.com"] != "000000" else "111111"
    r = await client.post("/login/email/verify", data={"csrf_token": csrf, "email": "a@example.com", "code": wrong, "name": "A"})
    assert "wrong or has expired" in r.text and client.cookies.get(SESSION_COOKIE) is None


@pytest.fixture
def google(monkeypatch):
    from app.web import router as web_router

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")
    profile = {"email": "Lawyer@Gmail.com", "email_verified": True, "name": "Adv. Google User"}

    async def fake_profile(code):
        assert code == "auth-code"
        return profile

    monkeypatch.setattr(web_router, "google_profile", fake_profile)
    return profile


def google_state(response) -> str:
    return re.search(r"state=([\w-]+)", response.headers["location"]).group(1)


async def test_google_login(client, db, google):
    r = await client.get("/login/google")
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    assert "redirect_uri=" in r.headers["location"]

    r = await client.get("/login/google/callback", params={"code": "auth-code", "state": google_state(r)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = await db.scalar(select(User).where(User.email == "lawyer@gmail.com"))
    assert user.name == "Adv. Google User" and user.email_verified and user.phone is None
    assert "Hello, Adv. Google User" in (await client.get("/")).text


async def test_google_login_rejects_bad_state_and_unverified_email(client, db, google):
    await client.get("/login/google")
    r = await client.get("/login/google/callback", params={"code": "auth-code", "state": "forged"})
    assert "sign you in with Google" in r.text and client.cookies.get(SESSION_COOKIE) is None
    google["email_verified"] = False
    r = await client.get("/login/google")
    r = await client.get("/login/google/callback", params={"code": "auth-code", "state": google_state(r)})
    assert "sign you in with Google" in r.text
    assert await db.scalar(select(User).where(User.email == "lawyer@gmail.com")) is None


async def test_google_and_email_reach_the_same_account(client, db, google, email_codes):
    r = await client.get("/login/google")
    await client.get("/login/google/callback", params={"code": "auth-code", "state": google_state(r)})
    client.cookies.delete(SESSION_COOKIE)
    await client.get("/login?method=email")
    csrf = client.cookies[CSRF_COOKIE]
    await client.post("/login/email/send", data={"csrf_token": csrf, "email": "lawyer@gmail.com"})
    r = await client.post("/login/email/verify", data={"csrf_token": csrf, "email": "lawyer@gmail.com", "code": email_codes["lawyer@gmail.com"]})
    assert r.status_code == 303
    assert len((await db.scalars(select(User).where(User.email == "lawyer@gmail.com"))).all()) == 1


def test_links_inside_forms_do_not_inherit_hx_disabled_elt():
    """htmx passes hx-disabled-elt down to links and hx-get dropdowns in the form; on those
    "find button" finds nothing and the request silently dies (this broke "Choose your court"
    and the state -> district picker)."""
    from pathlib import Path

    for path in (Path(__file__).parent.parent / "app" / "templates" / "web").glob("*.html"):
        for form in re.findall(r"<form[^>]*>.*?</form>", path.read_text(encoding="utf-8"), re.S):
            opening = form.split(">", 1)[0]
            inner = form.split(">", 1)[1]
            if "hx-disabled-elt" in opening and ("<a " in inner or "hx-get" in inner):
                assert "hx-disinherit" in opening, f"{path.name}: {opening[:80]}"
