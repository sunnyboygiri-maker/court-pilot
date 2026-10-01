"""
CourtPilot Telegram bot (python-telegram-bot, async).

Account linking is verified by Telegram itself: the user taps "Share phone
number", Telegram attaches their verified contact, and we check the contact
belongs to the sender. /link <phone> just pre-declares which number to expect,
so someone can't link a chat to another lawyer's account by typing their number.

Modes:
  - Webhook (production): the FastAPI app starts this bot in its lifespan when
    TELEGRAM_WEBHOOK_URL is set; updates arrive at POST /webhook/telegram.
  - Polling (development): python -m bot.telegram_bot
"""
import hashlib
import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.auth.phone import normalize_phone
from app.cases import service
from bot.message_templates import esc, fmt_date, format_case_summary
from config.settings import settings
from models.database import PLAN_LIMITS, PlanTier, User
from models.session import SessionLocal
from scraper.persist import case_title, normalize_cnr

logger = logging.getLogger("courtpilot.bot")

MAX_LIST = 20

HELP_TEXT = (
    "*CourtPilot commands*\n\n"
    "/start — link your account\n"
    "/link `<phone>` — link a specific phone number\n"
    "/track `<CNR>` — start tracking a case\n"
    "/untrack `<CNR>` — stop tracking a case\n"
    "/cases — your tracked cases\n"
    "/case `<CNR>` — details of one case\n"
    "/upcoming — hearings in the next 7 days\n"
    "/help — this message\n\n"
    "You'll get a weekly digest, reminders 3, 2 and 1 day before each hearing, "
    "and alerts when a new order is uploaded\\."
)


def webhook_secret() -> str:
    """Secret Telegram echoes in X-Telegram-Bot-Api-Secret-Token (A-Z, a-z, 0-9, _ and - only)."""
    if settings.TELEGRAM_WEBHOOK_SECRET:
        return settings.TELEGRAM_WEBHOOK_SECRET
    return hashlib.sha256(f"telegram-webhook:{settings.SECRET_KEY}".encode()).hexdigest()


def _share_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# --- Account helpers (DB) ---

async def get_linked_user(db: AsyncSession, chat_id: int) -> Optional[User]:
    return await db.scalar(select(User).where(User.telegram_chat_id == str(chat_id), User.is_active.is_(True)))


async def link_telegram_account(
    db: AsyncSession,
    phone: str,
    chat_id: int,
    username: Optional[str],
    display_name: str,
) -> tuple[User, bool]:
    """
    Link a verified phone to a Telegram chat, creating the account if needed.
    Returns (user, created).
    """
    # A chat maps to one account: unlink it from any other user first
    await db.execute(
        update(User)
        .where(User.telegram_chat_id == str(chat_id), User.phone != phone)
        .values(telegram_chat_id=None, telegram_username=None)
    )
    user = await db.scalar(select(User).where(User.phone == phone))
    created = False
    if user is None:
        user = User(
            phone=phone,
            name=display_name,
            plan=PlanTier.FREE,
            max_cases=PLAN_LIMITS[PlanTier.FREE]["max_cases"],
        )
        db.add(user)
        created = True
    elif not user.name:
        user.name = display_name
    user.telegram_chat_id = str(chat_id)
    user.telegram_username = username
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await link_telegram_account(db, phone, chat_id, username, display_name)
    return user, created


async def _require_user(update: Update, db: AsyncSession) -> Optional[User]:
    user = await get_linked_user(db, update.effective_chat.id)
    if user is None:
        await update.effective_message.reply_text(
            "Your Telegram isn't linked to a CourtPilot account yet. Tap the button below to link it.",
            reply_markup=_share_phone_keyboard(),
        )
    return user


async def _reply(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs
    )


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as db:
        user = await get_linked_user(db, update.effective_chat.id)
    if user:
        await _reply(update, f"Welcome back, {esc(user.name or 'Counsel')}\\!\n\n" + HELP_TEXT)
        return
    await update.effective_message.reply_text(
        "Welcome to CourtPilot ⚖️\n\n"
        "Get hearing reminders days in advance and alerts when new orders are uploaded — "
        "for every case you track on eCourts.\n\n"
        "To start, share your phone number so we can link your account.",
        reply_markup=_share_phone_keyboard(),
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        phone = normalize_phone(" ".join(context.args))
        if phone is None:
            await update.effective_message.reply_text("That doesn't look like a valid Indian mobile number.")
            return
        context.user_data["expected_phone"] = phone
        prompt = f"To confirm you own {phone}, tap the button below to share it from Telegram."
    else:
        prompt = "Tap the button below to share your phone number from Telegram."
    await update.effective_message.reply_text(prompt, reply_markup=_share_phone_keyboard())


async def contact_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = update.effective_message.contact
    sender = update.effective_user
    if contact is None or sender is None or contact.user_id != sender.id:
        await update.effective_message.reply_text(
            "Please share your own number using the button, not someone else's contact.",
            reply_markup=_share_phone_keyboard(),
        )
        return
    phone = normalize_phone(contact.phone_number)
    if phone is None:
        await update.effective_message.reply_text(
            "CourtPilot currently supports Indian mobile numbers only.", reply_markup=ReplyKeyboardRemove()
        )
        return
    expected = context.user_data.pop("expected_phone", None)
    if expected and expected != phone:
        await update.effective_message.reply_text(
            f"Your Telegram number is {phone}, not {expected}. Use /link with your Telegram number.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    display_name = " ".join(filter(None, [contact.first_name, contact.last_name])) or sender.full_name
    async with SessionLocal() as db:
        user, created = await link_telegram_account(db, phone, update.effective_chat.id, sender.username, display_name)
    greeting = "Account created and linked ✅" if created else "Account linked ✅"
    await update.effective_message.reply_text(
        f"{greeting}\n\nTrack your first case with /track <CNR>. Type /help for all commands.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        rows = await service.list_tracked(db, user.id, limit=MAX_LIST)
        total = await service.count_tracked(db, user.id)
        limit = service.case_limit(user)
    if not rows:
        await update.effective_message.reply_text("You aren't tracking any cases yet. Use /track <CNR> to add one.")
        return
    buttons = []
    for t in rows:
        c = t.court_case
        name = t.label or case_title(c)
        when = c.next_hearing_date.strftime("%d %b") if c.next_hearing_date else "—"
        buttons.append([InlineKeyboardButton(f"{when} · {name}"[:60], callback_data=f"case:{c.id}")])
    more = f"\n\\(showing first {MAX_LIST}\\)" if total > MAX_LIST else ""
    await _reply(
        update,
        f"*Your cases* — {total}/{limit} tracked{more}\nTap a case for details:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def case_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        case_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        try:
            tracked = await service.get_tracked(db, user.id, case_id)
        except service.CaseNotFound:
            await update.effective_message.reply_text("You're no longer tracking that case.")
            return
    await _reply(update, format_case_summary(tracked.court_case, tracked))


async def case_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /case <CNR>")
        return
    cnr = normalize_cnr(context.args[0])
    if cnr is None:
        await update.effective_message.reply_text("That isn't a valid CNR. It's 16 characters, e.g. DLHC010582482024.")
        return
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        try:
            tracked = await service.get_tracked_by_cnr(db, user.id, cnr)
        except service.CaseNotFound:
            await update.effective_message.reply_text(f"You aren't tracking {cnr}. Add it with /track {cnr}")
            return
    await _reply(update, format_case_summary(tracked.court_case, tracked))


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /track <CNR>\nThe CNR is the 16-character number on the eCourts case page.")
        return
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        await update.effective_message.reply_text("Looking up the case on eCourts… this can take up to a minute.")
        try:
            tracked = await service.track_case(db, user, context.args[0], service.get_scraper())
        except service.CaseServiceError as e:
            await update.effective_message.reply_text(str(e))
            return
    await _reply(update, "✅ *Now tracking*\n\n" + format_case_summary(tracked.court_case, tracked))


async def untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cnr = normalize_cnr(context.args[0]) if context.args else None
    if cnr is None:
        await update.effective_message.reply_text("Usage: /untrack <CNR>")
        return
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        try:
            tracked = await service.get_tracked_by_cnr(db, user.id, cnr)
            await service.untrack_case(db, user.id, tracked.case_id)
        except service.CaseNotFound:
            await update.effective_message.reply_text(f"You aren't tracking {cnr}.")
            return
    await update.effective_message.reply_text(f"Stopped tracking {cnr}.")


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as db:
        user = await _require_user(update, db)
        if not user:
            return
        rows = await service.upcoming(db, user.id, days=7)
    if not rows:
        await update.effective_message.reply_text("No hearings in the next 7 days. 🎉")
        return
    lines = ["*Hearings in the next 7 days*", ""]
    for t in rows:
        c = t.court_case
        lines.append(f"📅 *{esc(fmt_date(c.next_hearing_date))}*")
        lines.append(f"{esc(t.label or case_title(c))} — {esc(c.court_name)}")
        lines.append(f"`{esc(c.cnr_number)}`")
        lines.append("")
    await _reply(update, "\n".join(lines).rstrip())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Bot handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong. Please try again in a moment.")
        except Exception:
            pass


# --- Application wiring ---

def build_application(token: Optional[str] = None) -> Application:
    # Updates are handled concurrently so one slow eCourts lookup (/track)
    # doesn't block everyone else
    application = (
        ApplicationBuilder()
        .token(token or settings.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(8)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link", link))
    application.add_handler(CommandHandler("cases", cases))
    application.add_handler(CommandHandler("case", case_detail))
    application.add_handler(CommandHandler("track", track))
    application.add_handler(CommandHandler("untrack", untrack))
    application.add_handler(CommandHandler("upcoming", upcoming))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.CONTACT, contact_received))
    application.add_handler(CallbackQueryHandler(case_button, pattern=r"^case:\d+$"))
    application.add_error_handler(on_error)
    return application


async def start_webhook_bot() -> Application:
    """Start the bot for webhook mode; updates are fed in by app/webhooks/telegram.py."""
    application = build_application()
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=settings.TELEGRAM_WEBHOOK_URL,
        secret_token=webhook_secret(),
        allowed_updates=["message", "callback_query"],
    )
    logger.info("Telegram webhook set to %s", settings.TELEGRAM_WEBHOOK_URL)
    return application


async def stop_webhook_bot(application: Application) -> None:
    await application.stop()
    await application.shutdown()


def main() -> None:
    """Polling mode for local development."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    application = build_application()
    # run_polling removes any webhook registered for this token first
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
