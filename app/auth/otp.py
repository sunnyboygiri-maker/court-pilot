"""
One-time login codes, stored in Redis.

The same rules protect every kind of code (phone login, email login, confirming
a new email in Settings); `key` says which: "+91XXXXXXXXXX", "email:a@b.c",
"email-change:42".

Phone codes go by SMS when a gateway is configured (notifications/sms.py),
otherwise to the Telegram chat linked to that number. Email codes go by SMTP.
In DEBUG every code is also logged and returned to the caller.

Keys:
  otp:{key}           -> the code, TTL = OTP_TTL_SECONDS
  otp-attempts:{key}  -> verify attempts in the lockout window (not reset by resends)
  otp-sends:{key}     -> codes sent today, capped at OTP_MAX_SENDS_PER_DAY
  otp-cooldown:{key}  -> set on send, blocks resends for OTP_RESEND_COOLDOWN_SECONDS
"""
import hmac
import logging
import secrets
from typing import Awaitable, Callable, Optional

from redis.asyncio import Redis

from config.settings import settings
from notifications.sms import send_sms_otp, sms_configured

logger = logging.getLogger("courtpilot.auth")


class OTPError(Exception):
    pass


class OTPCooldown(OTPError):
    pass


class OTPNoChannel(OTPError):
    """No way to deliver a code (no SMS gateway, number not linked to Telegram)."""


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def email_configured() -> bool:
    return bool(settings.SMTP_USER and settings.SMTP_PASSWORD)


async def issue_code(redis: Redis, key: str, send: Callable[[str], Awaitable[None]]) -> str:
    """Rate-limit, generate, deliver with `send(code)`, then store. Returns the code."""
    if not await redis.set(f"otp-cooldown:{key}", 1, nx=True, ex=settings.OTP_RESEND_COOLDOWN_SECONDS):
        raise OTPCooldown("Please wait a minute before asking for another code")
    sends = await redis.incr(f"otp-sends:{key}")
    if sends == 1:
        await redis.expire(f"otp-sends:{key}", 24 * 3600)
    if sends > settings.OTP_MAX_SENDS_PER_DAY:
        raise OTPCooldown("Too many codes requested today. Please try again tomorrow")

    code = generate_otp()
    try:
        await send(code)
    except Exception:
        await redis.delete(f"otp-cooldown:{key}")
        raise
    # Stored only once delivered, so a failed send leaves nothing to guess
    await redis.set(f"otp:{key}", code, ex=settings.OTP_TTL_SECONDS)
    return code


async def verify_code(redis: Redis, key: str, code: str) -> bool:
    """
    Attempts are counted atomically *before* comparing, so parallel guesses
    can't exceed OTP_MAX_ATTEMPTS per lockout window, and resending a code
    doesn't reset the count. A correct code is single-use.
    """
    attempts_key = f"otp-attempts:{key}"
    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        await redis.expire(attempts_key, settings.OTP_LOCKOUT_SECONDS)
    if attempts > settings.OTP_MAX_ATTEMPTS:
        await redis.delete(f"otp:{key}")
        return False

    stored = await redis.get(f"otp:{key}")
    if stored is not None and hmac.compare_digest(stored, code):
        await redis.delete(f"otp:{key}", attempts_key)
        return True
    if attempts >= settings.OTP_MAX_ATTEMPTS:
        await redis.delete(f"otp:{key}")
    return False


# --- Phone ---

async def issue_otp(redis: Redis, phone: str, telegram_chat_id: Optional[str] = None) -> str:
    if not sms_configured() and not telegram_chat_id and not settings.DEBUG:
        raise OTPNoChannel("Link your Telegram account to receive login codes")
    return await issue_code(redis, phone, lambda code: deliver_otp(phone, code, telegram_chat_id))


async def deliver_otp(phone: str, code: str, telegram_chat_id: Optional[str] = None) -> None:
    if settings.DEBUG:
        logger.warning("DEBUG OTP for %s: %s", phone, code)
    if sms_configured():
        result = await send_sms_otp(phone, code)
        if not result.ok:
            raise OTPError("We couldn't send the SMS. Please try again in a minute")
        return
    if not telegram_chat_id or (settings.DEBUG and not settings.TELEGRAM_BOT_TOKEN):
        return  # DEBUG only (issue_otp refuses otherwise); the code is in the log
    from bot.message_templates import esc
    from notifications.telegram import send_telegram_message

    minutes = settings.OTP_TTL_SECONDS // 60
    result = await send_telegram_message(
        telegram_chat_id,
        f"🔐 Your CourtPilot login code is *{code}*\n\n"
        f"{esc(f'It expires in {minutes} minutes. Never share it with anyone, including CourtPilot staff.')}",
    )
    if not result.ok:
        logger.error("Telegram OTP delivery failed for %s: %s", phone, result.error)
        raise OTPError("We couldn't send your code on Telegram. Please try again")


async def verify_otp(redis: Redis, phone: str, code: str) -> bool:
    return await verify_code(redis, phone, code)


# --- Email ---

async def issue_email_code(redis: Redis, email: str, key: Optional[str] = None) -> str:
    """Email a code to `email`. `key` defaults to the login key for that address."""
    if not email_configured() and not settings.DEBUG:
        raise OTPNoChannel("Email codes aren't available right now")
    return await issue_code(redis, key or f"email:{email}", lambda code: deliver_email_code(email, code))


async def deliver_email_code(email: str, code: str) -> None:
    if settings.DEBUG:
        logger.warning("DEBUG email code for %s: %s", email, code)
        if not email_configured():
            return
    from notifications.email import render_email, send_email

    minutes = settings.OTP_TTL_SECONDS // 60
    html = render_email("login_code.html", code=code, minutes=minutes)
    text = f"Your {settings.APP_NAME} code is {code}. It expires in {minutes} minutes. If you didn't ask for it, ignore this email."
    result = await send_email(email, f"{code} is your {settings.APP_NAME} code", html, text)
    if not result.ok:
        raise OTPError("We couldn't send the email. Please check the address and try again")
