"""
Routes a notification to each channel the user has enabled and is entitled
to, and records every attempt in NotificationLog.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.service import has_whatsapp
from models.database import CourtCase, NotificationChannel, NotificationLog, NotificationType, TrackedCase, User
from notifications.base import DeliveryResult
from notifications.email import send_email
from notifications.telegram import send_telegram_message
from notifications.whatsapp import send_whatsapp_message

logger = logging.getLogger("courtpilot.notifications")


@dataclass
class NotificationContent:
    telegram_text: str
    email_subject: str
    email_html: str
    email_text: Optional[str] = None
    whatsapp_template: Optional[str] = None
    whatsapp_params: list = field(default_factory=list)


def enabled_channels(user: User, tracked: Optional[TrackedCase] = None) -> list[NotificationChannel]:
    """
    Channels to use for this user (and, if given, this tracked case).
    WhatsApp additionally requires an active add-on.
    """
    def wants(flag: str) -> bool:
        return tracked is None or bool(getattr(tracked, flag))

    channels = []
    if user.telegram_chat_id and wants("notify_telegram"):
        channels.append(NotificationChannel.TELEGRAM)
    # Only proven addresses, so nobody can point our emails at a stranger's inbox
    if user.email and user.email_verified and wants("notify_email"):
        channels.append(NotificationChannel.EMAIL)
    if has_whatsapp(user) and (user.whatsapp_number or user.phone) and wants("notify_whatsapp"):
        channels.append(NotificationChannel.WHATSAPP)
    return channels


async def _send(channel: NotificationChannel, user: User, content: NotificationContent) -> DeliveryResult:
    if channel == NotificationChannel.TELEGRAM:
        return await send_telegram_message(user.telegram_chat_id, content.telegram_text)
    if channel == NotificationChannel.EMAIL:
        return await send_email(user.email, content.email_subject, content.email_html, content.email_text)
    if not content.whatsapp_template:
        return DeliveryResult(False, "No WhatsApp template for this notification")
    return await send_whatsapp_message(
        user.whatsapp_number or user.phone, content.whatsapp_template, content.whatsapp_params
    )


async def dispatch_notification(
    db: AsyncSession,
    user: User,
    court_case: Optional[CourtCase],
    notification_type: NotificationType,
    content: NotificationContent,
    tracked: Optional[TrackedCase] = None,
    channels: Optional[list[NotificationChannel]] = None,
) -> dict[NotificationChannel, DeliveryResult]:
    """
    Send on every enabled channel. Adds NotificationLog rows to the session;
    the caller commits.
    """
    if channels is None:
        channels = enabled_channels(user, tracked)
    results: dict[NotificationChannel, DeliveryResult] = {}
    for channel in channels:
        try:
            result = await _send(channel, user, content)
        except Exception as e:  # never let one channel block the others
            logger.exception("%s delivery crashed for user %s", channel.value, user.id)
            result = DeliveryResult(False, f"Unexpected error: {e}")
        if not result.ok:
            logger.warning("%s delivery failed for user %s: %s", channel.value, user.id, result.error)
        results[channel] = result
        db.add(
            NotificationLog(
                user_id=user.id,
                case_id=court_case.id if court_case else None,
                channel=channel,
                notification_type=notification_type,
                subject=content.email_subject[:500],
                body=content.telegram_text,
                delivered=result.ok,
                error_message=result.error,
                cost_inr=result.cost_inr,
            )
        )
    return results
