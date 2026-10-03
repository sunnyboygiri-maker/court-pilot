"""
Incoming WhatsApp messages (Meta Cloud API webhook).

A lawyer sends a screenshot or photo of a case to CourtPilot's WhatsApp
number; it's read, looked up and added the same way as on Telegram
(search.jobs, kind "screenshot"), and the answer goes back on WhatsApp.

Meta needs, before this works: a verified WhatsApp Business account, this
URL on https (a real domain, not a bare IP), WHATSAPP_VERIFY_TOKEN and
WHATSAPP_APP_SECRET set, and the "messages" webhook field subscribed.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_, select

from app.auth.phone import normalize_phone
from app.redis import get_redis
from config.settings import settings
from models.database import User, UserCourt
from models.session import SessionLocal

logger = logging.getLogger("courtpilot.webhooks.whatsapp")

router = APIRouter(prefix="/webhook", tags=["webhooks"])

MAX_IMAGES_PER_DAY = 30
HELP = ("Send a screenshot or photo of a case, e.g. the eCourts app's My Cases screen, and CourtPilot "
        "will find it on eCourts and add it to your list.")


def _configured() -> bool:
    return bool(settings.WHATSAPP_VERIFY_TOKEN and settings.WHATSAPP_APP_SECRET and settings.WHATSAPP_ACCESS_TOKEN)


@router.get("/whatsapp", include_in_schema=False)
async def verify(request: Request):
    """Meta's one-time subscription check."""
    q = request.query_params
    if (_configured() and q.get("hub.mode") == "subscribe"
            and hmac.compare_digest(q.get("hub.verify_token", ""), settings.WHATSAPP_VERIFY_TOKEN)):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


def valid_signature(body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header or "", expected)


@router.post("/whatsapp", include_in_schema=False)
async def receive(request: Request, x_hub_signature_256: str = Header(default="")):
    if not _configured():
        raise HTTPException(404, "WhatsApp not enabled")
    body = await request.body()
    if not valid_signature(body, x_hub_signature_256):
        raise HTTPException(403, "Bad signature")
    payload = json.loads(body or b"{}")
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in (change.get("value") or {}).get("messages", []):
                # Answer Meta at once (it retries slow webhooks); work in the background
                asyncio.get_running_loop().create_task(handle_message(message))
    return {"ok": True}


async def _reply(to: str, text: str) -> None:
    from notifications.whatsapp import send_whatsapp_text

    result = await send_whatsapp_text(to, text)
    if not result.ok:
        logger.warning("WhatsApp reply failed: %s", result.error)


async def handle_message(message: dict) -> None:
    from notifications.whatsapp import download_whatsapp_media
    from search import jobs

    redis = get_redis()
    if not await redis.set(f"wa-msg:{message.get('id')}", 1, nx=True, ex=24 * 3600):
        return  # Meta delivered this one already
    sender = message.get("from", "")
    phone = normalize_phone(sender)
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(
            User.is_active.is_(True), or_(User.phone == phone, User.whatsapp_number == phone))) if phone else None
        if user is None:
            await _reply(sender, f"This WhatsApp number isn't on CourtPilot yet. Sign up at "
                                 f"{settings.APP_BASE_URL.rstrip('/')} with this number, then send the screenshot again.")
            return

        kind = message.get("type")
        media = message.get(kind) if kind in ("image", "document") else None
        if not media or (kind == "document" and not (media.get("mime_type") or "").startswith("image/")):
            await _reply(sender, HELP)
            return
        used = await redis.incr(f"img-count:{user.id}")
        if used == 1:
            await redis.expire(f"img-count:{user.id}", 24 * 3600)
        if used > MAX_IMAGES_PER_DAY:
            await _reply(sender, "That's the screenshot limit for today. Please try again tomorrow.")
            return
        try:
            data = await download_whatsapp_media(media["id"], jobs.MAX_IMAGE_BYTES)
        except Exception:
            logger.exception("Couldn't download WhatsApp media")
            await _reply(sender, "We couldn't open that image. Please send it again as a photo or screenshot.")
            return
        key = await jobs.store_image_async(redis, data)
        courts = (await db.scalars(select(UserCourt).where(UserCourt.user_id == user.id))).all()
        job = await jobs.create_job(db, user, "screenshot", {
            "images": [key], "courts": jobs.serialize_courts(courts), "whatsapp_to": re.sub(r"\D", "", sender),
        })
    jobs.start_job(job.id)
    await _reply(sender, "📷 Got it. Reading the case details and looking them up on eCourts. "
                         "Cases we're sure about are added to your list automatically.")
