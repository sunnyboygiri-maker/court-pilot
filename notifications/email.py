"""
Email delivery over SMTP with Jinja2 HTML templates. Free.
"""
import logging
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Optional

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import settings
from notifications.base import DeliveryResult

logger = logging.getLogger("courtpilot.notifications.email")

TEMPLATE_DIR = Path(__file__).parent / "templates"

env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_email(template_name: str, **context) -> str:
    context.setdefault("app_name", settings.APP_NAME)
    context.setdefault("app_base_url", settings.APP_BASE_URL.rstrip("/"))
    return env.get_template(template_name).render(**context)


async def send_email(to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> DeliveryResult:
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        return DeliveryResult(False, "SMTP not configured")

    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="courtpilot.in")
    msg.set_content(text_body or "This message needs an HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=settings.SMTP_PORT == 587,
            use_tls=settings.SMTP_PORT == 465,
            timeout=30,
        )
    except aiosmtplib.SMTPException as e:
        logger.warning("Email to %s failed: %s", to, e)
        return DeliveryResult(False, f"SMTP error: {e}")
    return DeliveryResult(True, provider_message_id=msg["Message-ID"])
