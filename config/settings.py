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
    # Wrong guesses count over this window and survive resends
    OTP_LOCKOUT_SECONDS: int = 3600
    OTP_MAX_SENDS_PER_DAY: int = 10
    # Web app session cookie lifetime
    WEB_SESSION_DAYS: int = 30
    # No payment gateway yet: self-service plan changes stay off in production
    ALLOW_PLAN_SELF_UPGRADE: bool = False

    # Database
    DATABASE_URL: str = "postgresql://courtpilot:courtpilot@localhost:5432/courtpilot"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    # Shown on the web app ("Open @<username>"); looked up from Telegram if empty
    TELEGRAM_BOT_USERNAME: str = ""
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

    # Login by SMS code. Empty = phone codes go to the user's linked Telegram chat.
    # "2factor" (2factor.in, SMS_TEMPLATE = template name, optional) or
    # "msg91" (SMS_TEMPLATE = DLT-approved OTP template id, required)
    SMS_PROVIDER: str = ""
    SMS_API_KEY: str = ""
    SMS_TEMPLATE: str = ""

    # "Continue with Google". Needs an OAuth client (Google Cloud console) whose
    # authorised redirect URI is {APP_BASE_URL}/login/google/callback; Google only
    # accepts https URLs on a real domain (or http://localhost for testing)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Scraping
    ECOURTS_POLL_INTERVAL_HOURS: int = 6
    ECOURTS_MAX_CONCURRENT: int = 5
    ECOURTS_RATE_LIMIT_PER_MINUTE: int = 30
    ECOURTS_MIN_REPOLL_HOURS: int = 4
    # The mobile-API fallback endpoint is unverified; keep it off unless confirmed
    ECOURTS_MOBILE_API_FALLBACK: bool = False
    # eCourts blocks many data-centre IPs ("405 Security Page"). Route its
    # traffic, and only its traffic, through this proxy, e.g.
    # http://user:pass@in.proxy.example:8000 (see scraper/proxy.py)
    ECOURTS_PROXY_URL: str = ""
    # "Find a case" searches run in the `searcher` Celery worker; true runs them
    # inside the API process instead (local preview / tests without Celery)
    SEARCH_INLINE: bool = False

    # Notification schedule (IST)
    WEEKLY_DIGEST_DAY: int = 0  # Monday
    WEEKLY_DIGEST_TIME: str = "08:00"
    REMINDER_EVENING_TIME: str = "19:00"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()


# Values that have appeared in this repo's docs/examples; never valid in production
PLACEHOLDER_SECRETS = {"change-me-in-production", "generate-a-secure-random-key", "secret", "changeme", ""}


def secret_key_problem(key: str) -> Optional[str]:
    """Why SECRET_KEY can't be used in production, or None if it's fine."""
    if key in PLACEHOLDER_SECRETS:
        return "SECRET_KEY is a placeholder value"
    if len(key) < 32:
        return "SECRET_KEY must be at least 32 characters"
    return None
