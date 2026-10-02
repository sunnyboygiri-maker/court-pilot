"""
Mapping between the scraper's normalized case dicts and CourtCase rows.

Used by both the API (first fetch on /cases/track) and the poller, so the
diff detector always compares like with like.
"""
import re
from datetime import date
from typing import Optional

from models.database import CaseStatus, CourtCase, CourtType

CNR_RE = re.compile(r"^[A-Z]{4}[0-9A-Z]{12}$")

DISTRICT_ECOURTS_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"
HC_ECOURTS_URL = "https://hcservices.ecourts.gov.in/hcservices/main.php"


def normalize_cnr(value: str) -> Optional[str]:
    """Uppercase and strip spaces/hyphens; None if it isn't a valid 16-char CNR."""
    cnr = re.sub(r"[\s-]", "", value or "").upper()
    return cnr if CNR_RE.match(cnr) else None


def parse_date(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def map_status(raw: Optional[str]) -> CaseStatus:
    text = (raw or "").lower()
    if "dispos" in text or "decided" in text:
        return CaseStatus.DISPOSED
    if "transfer" in text:
        return CaseStatus.TRANSFERRED
    if "pending" in text:
        return CaseStatus.PENDING
    return CaseStatus.UNKNOWN


def map_court_type(raw: Optional[str]) -> Optional[CourtType]:
    try:
        return CourtType(raw) if raw else None
    except ValueError:
        return None


def ecourts_link(court_case: CourtCase) -> str:
    if court_case.ecourts_url:
        return court_case.ecourts_url
    return HC_ECOURTS_URL if court_case.court_type == CourtType.HIGH_COURT else DISTRICT_ECOURTS_URL


def comparable(data: dict) -> dict:
    """
    Project scraper output onto the fields CaseDiffDetector watches, in the
    same representation case_to_dict() produces from the DB row.
    """
    return {
        "next_hearing_date": _iso(parse_date(data.get("next_hearing_date"))),
        "status": map_status(data.get("status")).value,
        "stage": data.get("stage"),
        "judge": data.get("judge"),
        "bench": data.get("bench"),
        "orders": data.get("orders") or [],
    }


def case_to_dict(court_case: CourtCase) -> dict:
    return {
        "next_hearing_date": _iso(court_case.next_hearing_date),
        "status": court_case.status.value if court_case.status else None,
        "stage": court_case.stage,
        "judge": court_case.judge,
        "bench": court_case.bench,
        "orders": court_case.orders_json or [],
    }


def apply_case_data(court_case: CourtCase, data: dict) -> None:
    """Copy normalized scraper output onto a CourtCase row."""
    court_case.case_type = data.get("case_type")
    court_case.case_number = data.get("case_number")
    court_case.filing_year = data.get("filing_year")
    court_case.filing_date = parse_date(data.get("filing_date"))
    court_case.registration_date = parse_date(data.get("registration_date"))
    court_case.court_type = map_court_type(data.get("court_type"))
    court_case.court_name = data.get("court_name")
    court_case.court_complex = data.get("court_complex")
    court_case.state = data.get("state")
    court_case.district = data.get("district")
    court_case.bench = data.get("bench")
    court_case.petitioner = data.get("petitioner")
    court_case.respondent = data.get("respondent")
    court_case.petitioner_advocate = data.get("petitioner_advocate")
    court_case.respondent_advocate = data.get("respondent_advocate")
    court_case.status = map_status(data.get("status"))
    court_case.next_hearing_date = parse_date(data.get("next_hearing_date"))
    court_case.previous_hearing_date = parse_date(data.get("previous_hearing_date"))
    court_case.stage = data.get("stage")
    court_case.judge = data.get("judge")
    court_case.acts_sections = data.get("acts_sections") or []
    orders = data.get("orders") or []
    court_case.orders_json = orders
    if orders:
        latest = max(orders, key=lambda o: str(o.get("date") or ""))
        court_case.latest_order_date = parse_date(latest.get("date"))
        court_case.latest_order_link = latest.get("link")
    court_case.raw_ecourts_data = data.get("raw_data")
    court_case.data_hash = data.get("data_hash")


def case_title(court_case: CourtCase) -> str:
    pet = _first_party(court_case.petitioner)
    res = _first_party(court_case.respondent)
    if pet and res:
        return f"{pet} vs {res}"
    return pet or res or court_case.cnr_number


def _first_party(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    first = value.split(",")[0].strip()
    return first + (" & Ors." if "," in value else "")


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None
