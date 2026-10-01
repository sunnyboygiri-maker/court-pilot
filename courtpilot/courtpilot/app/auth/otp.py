"""
Phone OTPs stored in Redis.

Keys:
  otp:{phone}           -> the code, TTL = OTP_TTL_SECONDS
  otp-attempts:{phone}  -> failed verify count, same TTL
  otp-cooldown:{phone}  -> set on send, blocks resends for OTP_RESEND_COOLDOWN_SECONDS
"""
import hmac
import logging
import secrets

from redis.asyncio import Redis

from config.settings import settings

logger = logging.getLogger("courtpilot.auth")


class OTPError(Exception):
    pass


class OTPCooldown(OTPError):
    pass


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_otp(redis: Redis, phone: str) -> str:
    if not await redis.set(f"otp-cooldown:{phone}", 1, nx=True, ex=settings.OTP_RESEND_COOLDOWN_SECONDS):
        raise OTPCooldown("Please wait before requesting another OTP")
    code = generate_otp()
    ttl = settings.OTP_TTL_SECONDS
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(f"otp:{phone}", code, ex=ttl)
        pipe.delete(f"otp-attempts:{phone}")
        await pipe.execute()
    await deliver_otp(phone, code)
    return code


async def deliver_otp(phone: str, code: str) -> None:
    """
    Send the OTP by SMS. No gateway is integrated yet (MSG91 planned);
    in DEBUG the code is logged instead.
    """
    if settings.DEBUG:
        logger.warning("DEBUG OTP for %s: %s", phone, code)
        return
    raise OTPError("SMS gateway not configured")


async def verify_otp(redis: Redis, phone: str, code: str) -> bool:
    """
    Check a code. Wrong codes count against OTP_MAX_ATTEMPTS, after which the
    OTP is burned. A correct code is single-use.

    In DEBUG mode any 6-digit code is accepted (see CLAUDE.md).
    """
    if settings.DEBUG and len(code) == 6 and code.isdigit():
        await redis.delete(f"otp:{phone}", f"otp-attempts:{phone}")
        return True

    stored = await redis.get(f"otp:{phone}")
    if stored is None:
        return False
    if hmac.compare_digest(stored, code):
        await redis.delete(f"otp:{phone}", f"otp-attempts:{phone}")
        return True

    attempts = await redis.incr(f"otp-attempts:{phone}")
    await redis.expire(f"otp-attempts:{phone}", settings.OTP_TTL_SECONDS)
    if attempts >= settings.OTP_MAX_ATTEMPTS:
        await redis.delete(f"otp:{phone}", f"otp-attempts:{phone}")
    return False
