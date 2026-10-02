from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.phone import normalize_phone
from app.cases.service import case_limit, count_tracked, effective_plan, has_whatsapp
from config.settings import settings
from config.timeutils import utcnow
from models.database import PLAN_LIMITS, WHATSAPP_ADDON_PRICE, PlanTier, TrackedCase, User
from models.session import get_db

router = APIRouter(prefix="/users", tags=["users"])

PLAN_PERIOD = timedelta(days=30)
REQUIRED_USER_FIELDS = {"name", "notification_time", "digest_day"}


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: Optional[str]
    email: Optional[str]
    email_verified: bool
    name: str
    bar_registration_no: Optional[str]
    state_bar_council: Optional[str]
    telegram_linked: bool
    telegram_username: Optional[str]
    whatsapp_number: Optional[str]
    plan: PlanTier
    whatsapp_addon: bool
    plan_expires_at: Optional[datetime]
    notification_time: str
    digest_day: int
    created_at: Optional[datetime]


class UpdateUserIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    bar_registration_no: Optional[str] = Field(default=None, max_length=50)
    state_bar_council: Optional[str] = Field(default=None, max_length=100)
    whatsapp_number: Optional[str] = None
    notification_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    digest_day: Optional[int] = Field(default=None, ge=0, le=6)

    @field_validator("whatsapp_number")
    @classmethod
    def valid_whatsapp(cls, v):
        if v is None:
            return v
        phone = normalize_phone(v)
        if phone is None:
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return phone


class ChannelPrefs(BaseModel):
    telegram: Optional[bool] = None
    email: Optional[bool] = None
    whatsapp: Optional[bool] = None


class NotificationPrefsIn(BaseModel):
    notification_time: Optional[str] = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="HH:MM IST, 24h"
    )
    digest_day: Optional[int] = Field(default=None, ge=0, le=6, description="0=Monday")
    channels: Optional[ChannelPrefs] = Field(
        default=None, description="Applied to all currently tracked cases"
    )


class PlanOut(BaseModel):
    plan: PlanTier
    effective_plan: PlanTier
    plan_expires_at: Optional[datetime]
    whatsapp_addon: bool
    max_cases: int
    cases_tracked: int
    price_monthly: int
    available_plans: dict[str, dict]
    whatsapp_addon_price: int


class UpgradeIn(BaseModel):
    plan: PlanTier
    whatsapp_addon: Optional[bool] = None


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        phone=user.phone,
        email=user.email,
        email_verified=bool(user.email_verified),
        name=user.name,
        bar_registration_no=user.bar_registration_no,
        state_bar_council=user.state_bar_council,
        telegram_linked=bool(user.telegram_chat_id),
        telegram_username=user.telegram_username,
        whatsapp_number=user.whatsapp_number,
        plan=user.plan,
        whatsapp_addon=bool(user.whatsapp_addon),
        plan_expires_at=user.plan_expires_at,
        notification_time=user.notification_time or "08:00",
        digest_day=user.digest_day if user.digest_day is not None else 0,
        created_at=user.created_at,
    )


async def _plan_out(db: AsyncSession, user: User) -> PlanOut:
    plan = effective_plan(user)
    addon = has_whatsapp(user)
    return PlanOut(
        plan=user.plan,
        effective_plan=plan,
        plan_expires_at=user.plan_expires_at,
        whatsapp_addon=addon,
        max_cases=case_limit(user),
        cases_tracked=await count_tracked(db, user.id),
        price_monthly=PLAN_LIMITS[plan]["price_monthly"] + (WHATSAPP_ADDON_PRICE if addon else 0),
        available_plans={tier.value: limits for tier, limits in PLAN_LIMITS.items()},
        whatsapp_addon_price=WHATSAPP_ADDON_PRICE,
    )


@router.get("/me", response_model=UserOut)
async def get_me(user: User = Depends(get_current_user)):
    return _user_out(user)


@router.put("/me", response_model=UserOut)
async def update_me(
    body: UpdateUserIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None and field in REQUIRED_USER_FIELDS:
            continue  # null would drop the user out of reminders (or violate NOT NULL)
        if field == "email":
            value = value.lower() if value else None
            if value != user.email:
                # A typed-in address is unproven: no login or reminders by email until verified
                user.email_verified = False
        setattr(user, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That email is already registered")
    await db.refresh(user)
    return _user_out(user)


@router.put("/me/notifications", response_model=UserOut)
async def update_notification_prefs(
    body: NotificationPrefsIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.notification_time is not None:
        user.notification_time = body.notification_time
    if body.digest_day is not None:
        user.digest_day = body.digest_day
    if body.channels is not None:
        values = {}
        if body.channels.telegram is not None:
            values["notify_telegram"] = body.channels.telegram
        if body.channels.email is not None:
            values["notify_email"] = body.channels.email
        if body.channels.whatsapp is not None:
            if body.channels.whatsapp and not has_whatsapp(user):
                raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "WhatsApp notifications need the WhatsApp add-on")
            values["notify_whatsapp"] = body.channels.whatsapp
        if values:
            await db.execute(update(TrackedCase).where(TrackedCase.user_id == user.id).values(**values))
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.get("/me/plan", response_model=PlanOut)
async def get_plan(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _plan_out(db, user)


@router.post("/me/plan/upgrade", response_model=PlanOut)
async def upgrade_plan(
    body: UpgradeIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    MVP: changes the tier directly for a 30-day period. No payment is taken —
    wire a payment gateway (Razorpay etc.) in front of this before launch.
    Disabled unless ALLOW_PLAN_SELF_UPGRADE is set, so nobody gets paid
    plans (or WhatsApp sends billed to us) for free.
    """
    if not settings.ALLOW_PLAN_SELF_UPGRADE:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Online payment is coming soon. Contact us to change your plan.")
    new_limit = PLAN_LIMITS[body.plan]["max_cases"]
    tracked = await count_tracked(db, user.id)
    if tracked > new_limit:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You track {tracked} cases; the {body.plan.value} plan allows {new_limit}. Untrack some first.",
        )

    user.plan = body.plan
    user.max_cases = new_limit
    addon = user.whatsapp_addon if body.whatsapp_addon is None else body.whatsapp_addon
    paid = body.plan != PlanTier.FREE or addon
    user.plan_expires_at = utcnow() + PLAN_PERIOD if paid else None

    if body.whatsapp_addon is not None and body.whatsapp_addon != user.whatsapp_addon:
        user.whatsapp_addon = body.whatsapp_addon
        # Turning the add-on on enables WhatsApp for existing cases; off disables it
        await db.execute(
            update(TrackedCase).where(TrackedCase.user_id == user.id).values(notify_whatsapp=body.whatsapp_addon)
        )
    await db.commit()
    await db.refresh(user)
    return await _plan_out(db, user)
