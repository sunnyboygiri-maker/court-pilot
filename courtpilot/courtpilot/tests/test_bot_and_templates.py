from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import bot.telegram_bot as tb
from app.cases import service
from bot import message_templates as mt
from config.timeutils import today_ist
from models.database import CourtCase, User
from notifications import content
from tests.conftest import CNR_A, CNR_B

RESERVED = set("_*[]()~`>#+-=|{}.!")
ENTITY_MARKERS = set("*_~`|[]()")


def assert_valid_markdown_v2(text: str) -> None:
    """
    Approximate MarkdownV2 lint: every reserved character outside code spans
    and link URLs must be escaped unless it's a formatting marker, and
    bold markers must pair up.
    """
    i, bold = 0, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            assert i + 1 < len(text), "dangling backslash"
            i += 2
            continue
        if c == "`":
            end = text.index("`", i + 1)
            i = end + 1
            continue
        if c == "]" and text[i + 1 : i + 2] == "(":
            j = i + 2
            while text[j] != ")":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "*":
            bold += 1
        if c in RESERVED and c not in ENTITY_MARKERS:
            raise AssertionError(f"unescaped {c!r} at {i}: …{text[max(0, i - 20):i + 20]}…")
        i += 1
    assert bold % 2 == 0, "unbalanced *"


@pytest.fixture
async def tracked_case(db, user, scraper):
    user.telegram_chat_id = "555"
    await db.commit()
    tracked = await service.track_case(db, user, CNR_A, scraper, label="Kumar (bail) - urgent!", client_name="R. Kumar")
    return tracked


async def test_templates_are_valid_markdown_v2(tracked_case):
    case = tracked_case.court_case
    texts = [
        mt.format_case_summary(case, tracked_case),
        mt.format_case_summary(case),
        mt.format_hearing_reminder(case, 1, tracked_case),
        mt.format_hearing_reminder(case, 3),
        mt.format_weekly_digest([(case, tracked_case), (case, None)], today_ist()),
        mt.format_new_order_alert(case, {"date": "2024-03-01", "description": "Interim Order", "link": "https://x.test/a_(1).pdf"}),
        mt.format_case_update(case, {"next_hearing_date": {"old": "2024-05-01", "new": "2024-06-01"}, "status": {"old": "pending", "new": "disposed"}}),
        tb.HELP_TEXT,
    ]
    for text in texts:
        assert_valid_markdown_v2(text)
    assert "Kumar \\(bail\\) \\- urgent\\!" in texts[0]
    assert "https://x.test/a_(1\\).pdf" in texts[5]


def test_markdown_lint_catches_mistakes():
    with pytest.raises(AssertionError):
        assert_valid_markdown_v2("Hearing on 01.02.2024")


async def test_email_content_renders_and_escapes(tracked_case):
    case = tracked_case.court_case
    case.petitioner = "<script>alert(1)</script>"
    tracked_case.label = None
    c = content.hearing_reminder(case, 2, tracked_case)
    assert "Hearing in 2 days" in c.email_subject
    assert "<script>" not in c.email_html and "&lt;script&gt;" in c.email_html
    assert f"https://courtpilot.test/case/{CNR_A}/view" in c.email_html
    assert c.whatsapp_template == "hearing_reminder" and len(c.whatsapp_params) == 4

    d = content.weekly_digest([(case, tracked_case)], today_ist())
    assert "1 listed" in d.email_subject and "Your week in court" in d.email_html
    assert content.new_order(case, {"date": "2024-03-01", "link": "https://x.test/o.pdf"}).whatsapp_params[1] == "https://x.test/o.pdf"
    u = content.case_update(case, {"stage": {"old": "Evidence", "new": "Arguments"}})
    assert "Stage: Evidence → Arguments" in u.email_html


# --- Bot handlers ---

def make_update(chat_id=555, user_id=42, contact=None, args=None):
    message = SimpleNamespace(reply_text=AsyncMock(), contact=contact)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, username="advtest", full_name="Adv Test"),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(args=args or [], user_data={})
    return update, context


def replies(update) -> list[str]:
    return [call.args[0] for call in update.effective_message.reply_text.call_args_list]


@pytest.fixture(autouse=True)
def bot_db(monkeypatch, session_factory, scraper):
    monkeypatch.setattr(tb, "SessionLocal", session_factory)
    monkeypatch.setattr(service, "get_scraper", lambda: scraper)


async def test_contact_share_creates_and_links_account(db):
    contact = SimpleNamespace(user_id=42, phone_number="919812345678", first_name="Priya", last_name="Rao")
    update, ctx = make_update(contact=contact)
    await tb.contact_received(update, ctx)
    assert "Account created and linked" in replies(update)[0]
    user = await db.scalar(select(User).where(User.phone == "+919812345678"))
    assert user.telegram_chat_id == "555" and user.name == "Priya Rao" and user.telegram_username == "advtest"


async def test_cannot_link_someone_elses_contact(db, user):
    contact = SimpleNamespace(user_id=999, phone_number=user.phone, first_name="X", last_name=None)
    update, ctx = make_update(contact=contact)
    await tb.contact_received(update, ctx)
    await db.refresh(user)
    assert user.telegram_chat_id is None
    assert "your own number" in replies(update)[0]


async def test_link_command_requires_matching_contact(db, user):
    update, ctx = make_update(args=["98765", "43210"])
    await tb.link(update, ctx)
    assert ctx.user_data["expected_phone"] == "+919876543210"

    other = SimpleNamespace(user_id=42, phone_number="+919000000000", first_name="A", last_name=None)
    update.effective_message.contact = other
    await tb.contact_received(update, ctx)
    assert "not +919876543210" in replies(update)[-1]
    await db.refresh(user)
    assert user.telegram_chat_id is None

    ctx.user_data["expected_phone"] = "+919876543210"
    update.effective_message.contact = SimpleNamespace(user_id=42, phone_number="+919876543210", first_name="A", last_name=None)
    await tb.contact_received(update, ctx)
    await db.refresh(user)
    assert user.telegram_chat_id == "555"


async def test_relinking_chat_moves_it_between_accounts(db, make_user):
    a = await make_user(phone="+919111111111", telegram_chat_id="555")
    b, created = await tb.link_telegram_account(db, "+919222222222", 555, None, "B")
    await db.refresh(a)
    assert created and a.telegram_chat_id is None and b.telegram_chat_id == "555"


async def test_unlinked_user_prompted_to_link():
    update, ctx = make_update()
    await tb.cases(update, ctx)
    assert "isn't linked" in replies(update)[0]


async def test_track_cases_case_upcoming_untrack(db, user):
    user.telegram_chat_id = "555"
    await db.commit()

    update, ctx = make_update(args=[CNR_A.lower()])
    await tb.track(update, ctx)
    assert "Now tracking" in replies(update)[-1]
    assert_valid_markdown_v2(replies(update)[-1])

    update, ctx = make_update(args=[CNR_B])
    await tb.track(update, ctx)

    update, ctx = make_update()
    await tb.cases(update, ctx)
    call = update.effective_message.reply_text.call_args
    assert "2/5 tracked" in call.args[0]
    buttons = call.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 2 and buttons[0][0].callback_data.startswith("case:")

    update, ctx = make_update(args=[CNR_A])
    await tb.case_detail(update, ctx)
    assert CNR_A in replies(update)[0]

    update, ctx = make_update()
    await tb.upcoming(update, ctx)
    text = replies(update)[0]
    assert CNR_A in text and CNR_B not in text
    assert_valid_markdown_v2(text)

    update, ctx = make_update(args=[CNR_A])
    await tb.untrack(update, ctx)
    assert replies(update)[0] == f"Stopped tracking {CNR_A}."

    update, ctx = make_update(args=[CNR_A])
    await tb.case_detail(update, ctx)
    assert "aren't tracking" in replies(update)[0]


async def test_track_reports_plan_limit(db, user, scraper):
    from tests.conftest import make_detail

    user.telegram_chat_id = "555"
    await db.commit()
    for i in range(5):
        cnr = f"DLWE01000{i}002024"
        scraper.details[cnr] = make_detail(cnr)
        await service.track_case(db, user, cnr, scraper)
    update, ctx = make_update(args=[CNR_B])
    await tb.track(update, ctx)
    assert "Upgrade to track more" in replies(update)[-1]


# --- Webhook ---

async def test_webhook_checks_secret(client):
    from app.main import app

    app.state.telegram_app = SimpleNamespace(bot=None, update_queue=SimpleNamespace(put=AsyncMock()))
    try:
        r = await client.post("/webhook/telegram", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        assert r.status_code == 403
        r = await client.post("/webhook/telegram", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": tb.webhook_secret()})
        assert r.status_code == 200
        app.state.telegram_app.update_queue.put.assert_awaited_once()
    finally:
        app.state.telegram_app = None


# --- Case view page ---

async def test_case_view_page(client, db, tracked_case):
    case = await db.scalar(select(CourtCase))
    case.next_hearing_date = today_ist() + timedelta(days=1)
    await db.commit()
    r = await client.get(f"/case/{CNR_A.lower()}/view")
    assert r.status_code == 200
    html = r.text
    assert "Ramesh Kumar &amp; Ors. vs State of NCT of Delhi" in html
    assert "Tomorrow" in html
    assert "https://ecourts.example/o0.pdf" in html
    assert "Open on eCourts" in html and "services.ecourts.gov.in" in html
    assert "Evidence" in html  # from hearing history timeline

    assert (await client.get("/case/KAMY010000012020/view")).status_code == 404
    assert (await client.get("/case/garbage/view")).status_code == 404


async def test_health(client, redis, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "get_redis", lambda: redis)
    monkeypatch.setattr(main, "SessionLocal", tb.SessionLocal)
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "ok", "redis": "ok"}
