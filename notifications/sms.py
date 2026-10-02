"""
Login codes by SMS through an Indian OTP gateway. Paid per message.

Indian (TRAI DLT) rules require a registered sender ID and message template
before any SMS is delivered; both providers below handle that through their
dashboards. Configure with SMS_PROVIDER / SMS_API_KEY / SMS_TEMPLATE.
"""
import logging

import httpx

from config.settings import settings
from notifications.base import DeliveryResult

logger = logging.getLogger("courtpilot.notifications.sms")


def sms_configured() -> bool:
    return settings.SMS_PROVIDER in ("2factor", "msg91") and bool(settings.SMS_API_KEY)


async def send_sms_otp(phone: str, code: str) -> DeliveryResult:
    """`phone` is +91XXXXXXXXXX."""
    if not sms_configured():
        return DeliveryResult(False, "SMS gateway not configured")
    number = phone.lstrip("+")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if settings.SMS_PROVIDER == "2factor":
                # MANUAL endpoint: we generate and verify the code ourselves
                url = f"https://2factor.in/API/V1/{settings.SMS_API_KEY}/SMS/{number}/{code}"
                if settings.SMS_TEMPLATE:
                    url += f"/{settings.SMS_TEMPLATE}"
                resp = await client.get(url)
                ok = resp.json().get("Status") == "Success"
            else:
                resp = await client.post(
                    "https://control.msg91.com/api/v5/otp",
                    params={"template_id": settings.SMS_TEMPLATE, "mobile": number, "otp": code,
                            "otp_expiry": max(1, settings.OTP_TTL_SECONDS // 60)},
                    headers={"authkey": settings.SMS_API_KEY, "accept": "application/json"},
                    json={},
                )
                ok = resp.json().get("type") == "success"
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("SMS OTP to %s failed: %s", phone, e)
        return DeliveryResult(False, f"SMS request failed: {e}")
    if not ok:
        logger.warning("SMS OTP to %s rejected: %s", phone, resp.text[:300])
        return DeliveryResult(False, f"SMS provider error: {resp.text[:200]}")
    return DeliveryResult(True)
