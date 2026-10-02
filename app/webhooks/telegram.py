import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from telegram import Update

from bot.telegram_bot import webhook_secret

logger = logging.getLogger("courtpilot.webhooks")

router = APIRouter(prefix="/webhook", tags=["webhooks"])


@router.post("/telegram", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    application = getattr(request.app.state, "telegram_app", None)
    if application is None:
        raise HTTPException(404, "Telegram webhook not enabled")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", webhook_secret()):
        raise HTTPException(403, "Bad secret token")

    update = Update.de_json(await request.json(), application.bot)
    # Queue rather than process inline: handlers like /track can take a
    # minute (CAPTCHA solving), and Telegram retries slow webhook responses
    await application.update_queue.put(update)
    return {"ok": True}
