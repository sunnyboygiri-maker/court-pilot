from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_NAME: str = "CourtPilot"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    # Public base URL, used for case-view links in notifications
    APP_BASE_URL: str = "https://courtpilot.in"

    # Auth
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    OTP_TTL_SECONDS: int = 300
    OTP_MAX_ATTEMPTS: int = 5
    OTP_RESEND_COOLDOWN_SECONDS: int = 60

    # Database
    DATABASE_URL: str = "postgresql://courtpilot:courtpilot@localhost:5432/courtpilot"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_URL: Optional[str] = None
    # Sent by Telegram in X-Telegram-Bot-Api-Secret-Token; derived from SECRET_KEY if unset
    TELEGRAM_WEBHOOK_SECRET: Optional[str] = None

    # WhatsApp Business API
    WHATSAPP_API_URL: str = "https://graph.facebook.com/v20.0"
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en"
    WHATSAPP_COST_PER_MESSAGE_INR: float = 0.80

    # Email (SMTP)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "CourtPilot <notifications@courtpilot.in>"

    # Scraping
    ECOURTS_POLL_INTERVAL_HOURS: int = 6
    ECOURTS_MAX_CONCURRENT: int = 5
    ECOURTS_RATE_LIMIT_PER_MINUTE: int = 30
    ECOURTS_MIN_REPOLL_HOURS: int = 4
    # The mobile-API fallback endpoint is unverified; keep it off unless confirmed
    ECOURTS_MOBILE_API_FALLBACK: bool = False

    # Notification schedule (IST)
    WEEKLY_DIGEST_DAY: int = 0  # Monday
    WEEKLY_DIGEST_TIME: str = "08:00"
    REMINDER_EVENING_TIME: str = "19:00"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
