"""
Telegram delivery via the Bot HTTP API. Free: no per-message charges.

Talks to the API with httpx directly rather than through python-telegram-bot's
Application, so Celery workers don't need a running bot.
"""
import asyncio
import logging

import httpx

from config.settings import settings
from notifications.base import DeliveryResult, SlidingWindowLimiter

logger = logging.getLogger("courtpilot.notifications.telegram")

API_BASE = "https://api.telegram.org"

# Telegram allows ~30 messages/second across different chats
_limiter = SlidingWindowLimiter(rate=30, per=1.0)


async def _call(method: str, payload: dict, retries: int = 2) -> DeliveryResult:
    if not settings.TELEGRAM_BOT_TOKEN:
        return DeliveryResult(False, "TELEGRAM_BOT_TOKEN not configured")
    url = f"{API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        for attempt in range(retries + 1):
            await _limiter.acquire()
            try:
                resp = await client.post(url, json=payload)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return DeliveryResult(False, f"Telegram request failed: {e}")

            if data.get("ok"):
                return DeliveryResult(True, provider_message_id=str(data["result"].get("message_id")))

            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after and attempt < retries:
                logger.warning("Telegram rate limited; sleeping %ss", retry_after)
                await asyncio.sleep(float(retry_after))
                continue
            return DeliveryResult(False, f"Telegram error {data.get('error_code')}: {data.get('description')}")
    return DeliveryResult(False, "Telegram request failed")


async def send_telegram_message(chat_id: str, text: str, parse_mode: str = "MarkdownV2",
                                reply_markup: dict | None = None) -> DeliveryResult:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "link_preview_options": {"is_disabled": True},
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _call("sendMessage", payload)


async def send_telegram_document(
    chat_id: str, file_url: str, caption: str = "", parse_mode: str = "MarkdownV2"
) -> DeliveryResult:
    """Send a document by URL (e.g. an order PDF); Telegram fetches it itself."""
    return await _call(
        "sendDocument",
        {"chat_id": chat_id, "document": file_url, "caption": caption, "parse_mode": parse_mode},
    )
