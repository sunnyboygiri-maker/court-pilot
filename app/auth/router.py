from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import otp
from app.auth.jwt import REFRESH, TokenError, create_access_token, create_refresh_token, decode_token
from app.auth.phone import normalize_phone
from app.redis import get_redis
from config.settings import settings
from models.database import PLAN_LIMITS, PlanTier, User
from models.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class PhoneIn(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, v: str) -> str:
        phone = normalize_phone(v)
        if phone is None:
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return phone


class SendOTPOut(BaseModel):
    message: str
    expires_in: int
    debug_otp: Optional[str] = None


class VerifyOTPIn(PhoneIn):
    otp: str = Field(pattern=r"^\d{6}$")
    name: Optional[str] = Field(default=None, max_length=255)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    is_new_user: bool = False


def _tokens(user_id: int, is_new_user: bool = False) -> TokenOut:
    return TokenOut(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
        user_id=user_id,
        is_new_user=is_new_user,
    )


async def telegram_chat_for(db: AsyncSession, phone: str) -> Optional[str]:
    """Where to deliver a login code for this phone, if anywhere."""
    return await db.scalar(
        select(User.telegram_chat_id).where(User.phone == phone, User.is_active.is_(True))
    )


@router.post("/send-otp", response_model=SendOTPOut)
async def send_otp(body: PhoneIn, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    try:
        code = await otp.issue_otp(redis, body.phone, await telegram_chat_for(db, body.phone))
    except otp.OTPCooldown as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e))
    except otp.OTPError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    return SendOTPOut(
        message="OTP sent",
        expires_in=settings.OTP_TTL_SECONDS,
        debug_otp=code if settings.DEBUG else None,
    )


@router.post("/verify-otp", response_model=TokenOut)
async def verify_otp(
    body: VerifyOTPIn,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    if not await otp.verify_otp(redis, body.phone, body.otp):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired OTP")

    user, created = await get_or_create_user(db, body.phone, body.name)
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    return _tokens(user.id, is_new_user=created)


async def get_or_create_user(db: AsyncSession, phone: str, name: Optional[str]) -> tuple[User, bool]:
    """After a verified OTP: the account for this phone, created on first login."""
    user = await db.scalar(select(User).where(User.phone == phone))
    if user is not None:
        return user, False
    user = User(
        phone=phone,
        name=(name or "").strip(),
        plan=PlanTier.FREE,
        max_cases=PLAN_LIMITS[PlanTier.FREE]["max_cases"],
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Same phone verified concurrently
        await db.rollback()
        return await db.scalar(select(User).where(User.phone == phone)), False
    return user, True


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    try:
        user_id = decode_token(body.refresh_token, REFRESH)
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    return _tokens(user.id)


async def get_or_create_by_email(db: AsyncSession, email: str, name: Optional[str]) -> tuple[User, bool]:
    """
    After the user proved they own `email` (email code or Google): their account,
    created on first login.

    An address someone typed into their profile without proving it doesn't
    count: the proven owner gets their own account and the address moves to it.
    Otherwise anyone could add a colleague's email to their own account and
    have the colleague's Google login land in it.
    """
    email = email.strip().lower()
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if user is not None and user.email_verified:
        return user, False
    if user is not None:
        user.email = None
        await db.flush()
    user = User(
        email=email,
        email_verified=True,
        name=(name or "").strip() or email.split("@")[0],
        plan=PlanTier.FREE,
        max_cases=PLAN_LIMITS[PlanTier.FREE]["max_cases"],
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await db.scalar(select(User).where(func.lower(User.email) == email)), False
    return user, True
