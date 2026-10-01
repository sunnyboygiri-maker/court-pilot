from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.database import CaseStatus, CourtType


class TrackCaseIn(BaseModel):
    cnr_number: str = Field(min_length=16, max_length=25)
    label: Optional[str] = Field(default=None, max_length=255)
    client_name: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None
    priority: int = Field(default=0, ge=0, le=2)
    is_petitioner_side: Optional[bool] = None


class UpdateTrackedCaseIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None
    client_name: Optional[str] = Field(default=None, max_length=255)
    priority: Optional[int] = Field(default=None, ge=0, le=2)
    is_petitioner_side: Optional[bool] = None
    notify_telegram: Optional[bool] = None
    notify_email: Optional[bool] = None
    notify_whatsapp: Optional[bool] = None


class SearchAdvocateIn(BaseModel):
    advocate_name: Optional[str] = Field(default=None, min_length=3, max_length=255)
    bar_code: Optional[str] = Field(default=None, max_length=50, description='e.g. "D/1234/2010"')
    state: str = Field(description='High Court code ("delhi"), court name, or state name')


class CourtCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cnr_number: str
    title: str
    case_type: Optional[str]
    case_number: Optional[str]
    filing_year: Optional[int]
    court_type: Optional[CourtType]
    court_name: Optional[str]
    state: Optional[str]
    district: Optional[str]
    bench: Optional[str]
    petitioner: Optional[str]
    respondent: Optional[str]
    status: Optional[CaseStatus]
    next_hearing_date: Optional[date]
    previous_hearing_date: Optional[date]
    stage: Optional[str]
    judge: Optional[str]
    latest_order_date: Optional[date]
    latest_order_link: Optional[str]
    last_polled_at: Optional[datetime]
    ecourts_url: str
    view_url: str


class CourtCaseDetailOut(CourtCaseOut):
    filing_date: Optional[date]
    registration_date: Optional[date]
    court_complex: Optional[str]
    petitioner_advocate: Optional[str]
    respondent_advocate: Optional[str]
    acts_sections: Optional[list[dict[str, Any]]]
    orders: list[dict[str, Any]] = []


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    changes: Optional[dict[str, Any]]
    captured_at: Optional[datetime]


class TrackedCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tracking_id: int
    label: Optional[str]
    notes: Optional[str]
    client_name: Optional[str]
    is_petitioner_side: Optional[bool]
    priority: int
    notify_telegram: bool
    notify_email: bool
    notify_whatsapp: bool
    tracked_since: Optional[datetime]
    case: CourtCaseOut


class TrackedCaseDetailOut(TrackedCaseOut):
    case: CourtCaseDetailOut
    snapshots: list[SnapshotOut] = []


class AdvocateSearchResult(BaseModel):
    cnr_number: str
    case_type: Optional[str]
    case_number: Optional[str]
    petitioner: Optional[str]
    respondent: Optional[str]
    status: Optional[str]
    next_hearing_date: Optional[str]
    court_name: Optional[str]
    already_tracked: bool
