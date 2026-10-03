"""
WhatsApp delivery via Meta Cloud API. PAID add-on (₹199/month) because Meta
charges per template message.

Business-initiated messages must use templates pre-approved in WhatsApp
Manager. Register these (category: Utility, language: WHATSAPP_TEMPLATE_LANGUAGE)
before going live, with body parameters in this order:

  hearing_reminder: "Reminder: {{1}} hearing on {{2}} at {{3}}. Details: {{4}}"
  weekly_digest:    "Weekly Case Summary: {{1}} hearings this week. {{2}}"
  new_order:        "New order uploaded in {{1}}. View: {{2}}"
  case_update:      "Update in {{1}}: {{2}}. Details: {{3}}"
"""
import logging
import re

import httpx

from config.settings import settings
from notifications.base import DeliveryResult

logger = logging.getLogger("courtpilot.notifications.whatsapp")

TEMPLATES = {"hearing_reminder", "weekly_digest", "new_order", "case_update"}


def _clean_param(value) -> str:
    # Template params may not contain newlines, tabs or 4+ consecutive spaces
    text = re.sub(r"\s+", " ", str(value or "-")).strip()
    return text[:1000] or "-"


async def send_whatsapp_message(phone: str, template_name: str, parameters: list) -> DeliveryResult:
    if template_name not in TEMPLATES:
        return DeliveryResult(False, f"Unknown WhatsApp template {template_name!r}")
    if not settings.WHATSAPP_PHONE_NUMBER_ID or not settings.WHATSAPP_ACCESS_TOKEN:
        return DeliveryResult(False, "WhatsApp API not configured")

    url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": re.sub(r"\D", "", phone),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": settings.WHATSAPP_TEMPLATE_LANGUAGE},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": _clean_param(p)} for p in parameters],
                }
            ],
        },
    }
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return DeliveryResult(False, f"WhatsApp request failed: {e}")

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        return DeliveryResult(False, f"WhatsApp error {err.get('code')}: {err.get('message')}")
    message_id = (data.get("messages") or [{}])[0].get("id")
    # Meta bills per accepted template message
    return DeliveryResult(True, cost_inr=settings.WHATSAPP_COST_PER_MESSAGE_INR, provider_message_id=message_id)


def _graph_headers() -> dict:
    return {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}


async def send_whatsapp_text(phone: str, text: str) -> DeliveryResult:
    """
    Free-form reply. Only allowed within 24 hours of the person's own message
    (Meta's customer-service window), which is exactly when we use it: answering
    a screenshot a lawyer just sent.
    """
    if not settings.WHATSAPP_PHONE_NUMBER_ID or not settings.WHATSAPP_ACCESS_TOKEN:
        return DeliveryResult(False, "WhatsApp API not configured")
    url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": re.sub(r"\D", "", phone), "type": "text",
               "text": {"body": text[:4000], "preview_url": False}}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=_graph_headers())
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return DeliveryResult(False, f"WhatsApp request failed: {e}")
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        return DeliveryResult(False, f"WhatsApp error {err.get('code')}: {err.get('message')}")
    return DeliveryResult(True, provider_message_id=(data.get("messages") or [{}])[0].get("id"))


async def download_whatsapp_media(media_id: str, max_bytes: int) -> bytes:
    """Bytes of an image someone sent us (two calls: media URL, then the file)."""
    base = settings.WHATSAPP_API_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = (await client.get(f"{base}/{media_id}", headers=_graph_headers())).json()
        if int(meta.get("file_size") or 0) > max_bytes:
            raise ValueError("image too large")
        resp = await client.get(meta["url"], headers=_graph_headers())
        resp.raise_for_status()
        return resp.content
