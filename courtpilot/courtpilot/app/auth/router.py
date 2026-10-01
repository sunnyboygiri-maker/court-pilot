from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import select
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


@router.post("/send-otp", response_model=SendOTPOut)
async def send_otp(body: PhoneIn, redis: Redis = Depends(get_redis)):
    try:
        code = await otp.issue_otp(redis, body.phone)
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

    user = await db.scalar(select(User).where(User.phone == body.phone))
    if user is not None:
        if not user.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
        return _tokens(user.id)

    user = User(
        phone=body.phone,
        name=(body.name or "").strip(),
        plan=PlanTier.FREE,
        max_cases=PLAN_LIMITS[PlanTier.FREE]["max_cases"],
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Same phone verified concurrently
        await db.rollback()
        user = await db.scalar(select(User).where(User.phone == body.phone))
        return _tokens(user.id)
    return _tokens(user.id, is_new_user=True)


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
