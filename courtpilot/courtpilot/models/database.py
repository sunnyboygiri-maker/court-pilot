"""
CourtPilot Database Models
"""
import enum
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Date,
    ForeignKey, Enum, JSON, Index, UniqueConstraint, Float
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


# --- Enums ---

class PlanTier(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    FIRM = "firm"


class CourtType(str, enum.Enum):
    DISTRICT = "district"
    HIGH_COURT = "high_court"
    SUPREME_COURT = "supreme_court"
    TRIBUNAL = "tribunal"


class NotificationChannel(str, enum.Enum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class NotificationType(str, enum.Enum):
    WEEKLY_DIGEST = "weekly_digest"
    THREE_DAY = "three_day"
    TWO_DAY = "two_day"
    ONE_DAY = "one_day"
    NEW_ORDER = "new_order"
    CASE_UPDATE = "case_update"


class CaseStatus(str, enum.Enum):
    PENDING = "pending"
    DISPOSED = "disposed"
    TRANSFERRED = "transferred"
    UNKNOWN = "unknown"


# --- Users & Auth ---

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Auth
    phone = Column(String(15), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=True)  # nullable for OTP-only auth
    # Profile
    name = Column(String(255), nullable=False)
    bar_registration_no = Column(String(50), nullable=True, index=True)
    state_bar_council = Column(String(100), nullable=True)
    # Notification channels
    telegram_chat_id = Column(String(50), nullable=True, index=True)
    telegram_username = Column(String(100), nullable=True)
    whatsapp_number = Column(String(15), nullable=True)
    # Subscription
    plan = Column(Enum(PlanTier), default=PlanTier.FREE, nullable=False)
    whatsapp_addon = Column(Boolean, default=False)
    plan_expires_at = Column(DateTime, nullable=True)
    max_cases = Column(Integer, default=5)  # derived from plan
    # Preferences
    notification_time = Column(String(5), default="08:00")  # HH:MM
    timezone = Column(String(50), default="Asia/Kolkata")
    digest_day = Column(Integer, default=0)  # 0=Monday
    # Metadata
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    tracked_cases = relationship("TrackedCase", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("NotificationLog", back_populates="user", cascade="all, delete-orphan")


# --- Cases ---

class CourtCase(Base):
    """
    Master case record — one per CNR number.
    Shared across users; polled by the scraper.
    """
    __tablename__ = "court_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cnr_number = Column(String(25), unique=True, nullable=False, index=True)
    # Case identity
    case_type = Column(String(100), nullable=True)
    case_number = Column(String(50), nullable=True)
    filing_year = Column(Integer, nullable=True)
    filing_date = Column(Date, nullable=True)
    registration_date = Column(Date, nullable=True)
    # Court
    court_type = Column(Enum(CourtType), nullable=True)
    court_name = Column(String(255), nullable=True)
    court_complex = Column(String(255), nullable=True)
    state = Column(String(100), nullable=True)
    district = Column(String(100), nullable=True)
    bench = Column(String(255), nullable=True)
    # Parties
    petitioner = Column(Text, nullable=True)
    respondent = Column(Text, nullable=True)
    petitioner_advocate = Column(Text, nullable=True)
    respondent_advocate = Column(Text, nullable=True)
    # Status
    status = Column(Enum(CaseStatus), default=CaseStatus.UNKNOWN)
    next_hearing_date = Column(Date, nullable=True, index=True)
    previous_hearing_date = Column(Date, nullable=True)
    stage = Column(String(255), nullable=True)  # e.g. "Evidence", "Arguments"
    judge = Column(String(255), nullable=True)
    # Acts & sections
    acts_sections = Column(JSON, nullable=True)  # [{"act": "...", "section": "..."}]
    # Orders
    latest_order_date = Column(Date, nullable=True)
    latest_order_link = Column(Text, nullable=True)
    orders_json = Column(JSON, nullable=True)  # [{date, link, description}]
    # Raw data
    raw_ecourts_data = Column(JSON, nullable=True)
    ecourts_url = Column(Text, nullable=True)
    # Polling
    last_polled_at = Column(DateTime, nullable=True)
    poll_error_count = Column(Integer, default=0)
    data_hash = Column(String(64), nullable=True)  # SHA-256 of key fields for diff detection
    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    tracked_by = relationship("TrackedCase", back_populates="court_case")
    snapshots = relationship("CaseSnapshot", back_populates="court_case", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_court_cases_next_hearing", "next_hearing_date"),
        Index("ix_court_cases_poll", "last_polled_at", "poll_error_count"),
    )


class TrackedCase(Base):
    """
    Join table: which user is tracking which case.
    """
    __tablename__ = "tracked_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    case_id = Column(Integer, ForeignKey("court_cases.id", ondelete="CASCADE"), nullable=False)
    # User-specific metadata
    label = Column(String(255), nullable=True)  # user's nickname for the case
    notes = Column(Text, nullable=True)
    client_name = Column(String(255), nullable=True)
    is_petitioner_side = Column(Boolean, nullable=True)
    priority = Column(Integer, default=0)  # 0=normal, 1=high, 2=urgent
    # Notification prefs for this case
    notify_telegram = Column(Boolean, default=True)
    notify_email = Column(Boolean, default=True)
    notify_whatsapp = Column(Boolean, default=False)
    # Metadata
    created_at = Column(DateTime, server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="tracked_cases")
    court_case = relationship("CourtCase", back_populates="tracked_by")

    __table_args__ = (
        UniqueConstraint("user_id", "case_id", name="uq_user_case"),
        Index("ix_tracked_cases_user", "user_id"),
    )


class CaseSnapshot(Base):
    """
    Point-in-time snapshot for diff detection.
    Stored each time a change is detected during polling.
    """
    __tablename__ = "case_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("court_cases.id", ondelete="CASCADE"), nullable=False)
    data_hash = Column(String(64), nullable=False)
    snapshot_data = Column(JSON, nullable=False)
    # What changed
    changes = Column(JSON, nullable=True)  # {"next_hearing_date": {"old": "...", "new": "..."}, ...}
    # Metadata
    captured_at = Column(DateTime, server_default=func.now())

    court_case = relationship("CourtCase", back_populates="snapshots")


# --- Notifications ---

class NotificationLog(Base):
    __tablename__ = "notification_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    case_id = Column(Integer, ForeignKey("court_cases.id", ondelete="SET NULL"), nullable=True)
    channel = Column(Enum(NotificationChannel), nullable=False)
    notification_type = Column(Enum(NotificationType), nullable=False)
    # Content
    subject = Column(String(500), nullable=True)
    body = Column(Text, nullable=True)
    # Delivery
    sent_at = Column(DateTime, server_default=func.now())
    delivered = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    # Cost tracking
    cost_inr = Column(Float, default=0.0)

    user = relationship("User", back_populates="notifications")

    __table_args__ = (
        Index("ix_notification_log_user", "user_id", "sent_at"),
    )


# --- Plan limits lookup ---

PLAN_LIMITS = {
    PlanTier.FREE: {"max_cases": 5, "price_monthly": 0},
    PlanTier.STARTER: {"max_cases": 50, "price_monthly": 299},
    PlanTier.PRO: {"max_cases": 100, "price_monthly": 499},
    PlanTier.FIRM: {"max_cases": 500, "price_monthly": 1499},
}

WHATSAPP_ADDON_PRICE = 199  # per month
