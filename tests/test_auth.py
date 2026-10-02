import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth import otp
from app.auth.jwt import create_refresh_token
from app.auth.phone import normalize_phone
from config.settings import settings
from models.database import User


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("5876543210", None),  # Indian mobiles start 6-9
        ("12345", None),
        ("", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.fixture
def sent_otps(monkeypatch):
    sent = {}

    async def fake_deliver(phone, code, telegram_chat_id=None):
        sent[phone] = code
        sent[f"chat:{phone}"] = telegram_chat_id

    monkeypatch.setattr(otp, "deliver_otp", fake_deliver)
    return sent


@pytest_asyncio.fixture
async def linked_user(make_user):
    """Codes are delivered on Telegram, so the number must be linked to a chat."""
    return await make_user(telegram_chat_id="555")


async def test_debug_login_creates_user_then_logs_in(client, db, redis, sent_otps, monkeypatch):
    # Brand-new numbers can only get a code in DEBUG (in production the bot creates them)
    monkeypatch.setattr(settings, "DEBUG", True)
    r = await client.post("/auth/send-otp", json={"phone": "98765 43210"})
    assert r.status_code == 200
    code = sent_otps["+919876543210"]
    assert r.json()["debug_otp"] == code

    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": code, "name": "Adv. Mehta"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_new_user"] is True
    user = await db.scalar(select(User).where(User.phone == "+919876543210"))
    assert user.name == "Adv. Mehta" and user.max_cases == 5

    # OTP is single-use
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": code})
    assert r.status_code == 401

    # Second login returns the same account
    await redis.delete("otp-cooldown:+919876543210")
    await client.post("/auth/send-otp", json={"phone": "9876543210"})
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": sent_otps["+919876543210"]})
    assert r.json()["is_new_user"] is False and r.json()["user_id"] == body["user_id"]

    r = await client.get("/users/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 200 and r.json()["phone"] == "+919876543210"


async def test_otp_goes_to_linked_telegram_chat(client, linked_user, sent_otps):
    r = await client.post("/auth/send-otp", json={"phone": "9876543210"})
    assert r.status_code == 200 and r.json()["debug_otp"] is None  # never leaked outside DEBUG
    assert sent_otps["chat:+919876543210"] == "555"
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": sent_otps["+919876543210"]})
    assert r.status_code == 200 and r.json()["is_new_user"] is False


async def test_wrong_otp_burns_after_max_attempts(client, linked_user, sent_otps):
    await client.post("/auth/send-otp", json={"phone": "9876543210"})
    code = sent_otps["+919876543210"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(settings.OTP_MAX_ATTEMPTS):
        r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": wrong})
        assert r.status_code == 401
    # Even the right code fails now
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": code})
    assert r.status_code == 401


async def test_resend_does_not_reset_attempts(client, linked_user, redis, sent_otps):
    await client.post("/auth/send-otp", json={"phone": "9876543210"})
    wrong = "000000" if sent_otps["+919876543210"] != "000000" else "111111"
    for _ in range(settings.OTP_MAX_ATTEMPTS):
        await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": wrong})
    # A fresh code doesn't buy fresh guesses
    await redis.delete("otp-cooldown:+919876543210")
    await client.post("/auth/send-otp", json={"phone": "9876543210"})
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": sent_otps["+919876543210"]})
    assert r.status_code == 401


async def test_daily_send_cap(client, linked_user, redis, sent_otps):
    for _ in range(settings.OTP_MAX_SENDS_PER_DAY):
        await redis.delete("otp-cooldown:+919876543210")
        assert (await client.post("/auth/send-otp", json={"phone": "9876543210"})).status_code == 200
    await redis.delete("otp-cooldown:+919876543210")
    assert (await client.post("/auth/send-otp", json={"phone": "9876543210"})).status_code == 429


async def test_failed_delivery_stores_no_code(client, linked_user, redis, monkeypatch):
    async def broken(phone, code, telegram_chat_id=None):
        raise otp.OTPError("Telegram down")

    monkeypatch.setattr(otp, "deliver_otp", broken)
    assert (await client.post("/auth/send-otp", json={"phone": "9876543210"})).status_code == 503
    assert await redis.get("otp:+919876543210") is None
    assert await redis.get("otp-cooldown:+919876543210") is None  # can retry straight away


async def test_resend_cooldown(client, linked_user, sent_otps):
    assert (await client.post("/auth/send-otp", json={"phone": "9876543210"})).status_code == 200
    assert (await client.post("/auth/send-otp", json={"phone": "9876543210"})).status_code == 429


async def test_send_otp_without_telegram_link_is_503(client):
    r = await client.post("/auth/send-otp", json={"phone": "9876543210"})
    assert r.status_code == 503


async def test_debug_mode_no_longer_accepts_any_otp(client, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)
    r = await client.post("/auth/send-otp", json={"phone": "9876543210"})
    code = r.json()["debug_otp"]
    wrong = "123456" if code != "123456" else "654321"
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": wrong})
    assert r.status_code == 401
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": code, "name": "Adv. Test"})
    assert r.status_code == 200


async def test_invalid_phone_rejected(client):
    r = await client.post("/auth/send-otp", json={"phone": "12345"})
    assert r.status_code == 422


async def test_refresh(client, user):
    r = await client.post("/auth/refresh", json={"refresh_token": create_refresh_token(user.id)})
    assert r.status_code == 200
    access = r.json()["access_token"]
    assert (await client.get("/users/me", headers={"Authorization": f"Bearer {access}"})).status_code == 200


async def test_access_token_cannot_refresh_and_refresh_cannot_access(client, user, auth):
    access = auth["Authorization"].split()[1]
    assert (await client.post("/auth/refresh", json={"refresh_token": access})).status_code == 401
    refresh = create_refresh_token(user.id)
    assert (await client.get("/users/me", headers={"Authorization": f"Bearer {refresh}"})).status_code == 401


async def test_protected_routes_require_auth(client):
    assert (await client.get("/cases/")).status_code == 401
    assert (await client.get("/users/me", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_phone_codes_go_by_sms_when_a_gateway_is_set(client, monkeypatch):
    """With SMS configured, phone login works without Telegram."""
    from notifications.base import DeliveryResult

    sent = {}

    async def fake_sms(phone, code):
        sent[phone] = code
        return DeliveryResult(True)

    monkeypatch.setattr(otp, "sms_configured", lambda: True)
    monkeypatch.setattr(otp, "send_sms_otp", fake_sms)
    r = await client.post("/auth/send-otp", json={"phone": "9876543210"})
    assert r.status_code == 200
    r = await client.post("/auth/verify-otp", json={"phone": "9876543210", "otp": sent["+919876543210"], "name": "Adv. SMS"})
    assert r.status_code == 200 and r.json()["is_new_user"] is True
