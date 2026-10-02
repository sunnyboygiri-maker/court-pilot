"""
eCourts Case Scraper — uses bharat-courts SDK for data retrieval.
Handles polling, diff detection, and triggering notifications.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from scraper.proxy import install as _install_proxy, proxy_for_httpx

logger = logging.getLogger("courtpilot.scraper")

# CNR lookups and polling go through ECOURTS_PROXY_URL when it's set
_install_proxy()


class CaseNotFoundError(ValueError):
    """The CNR is malformed, or no court returned data for it. Not retried."""


def _join(values) -> Optional[str]:
    joined = ", ".join(v for v in values if v)
    return joined or None


class ECourtsScraper:
    """
    Scrapes case data from eCourts using the bharat-courts SDK.

    Usage:
        scraper = ECourtsScraper()
        case_data = await scraper.fetch_case_by_cnr("DLWE010012345202X")
        cases = await scraper.fetch_cases_by_advocate("Adv. Name", state="Delhi")
    """

    # eCourts service endpoints (discovered from mobile app traffic)
    DISTRICT_COURT_BASE = "https://services.ecourts.gov.in/ecourtindia_v6"
    HIGH_COURT_BASE = "https://hcservices.ecourts.gov.in/ecourtindiaHC"

    # Mobile app API endpoints (no CAPTCHA)
    MOBILE_API_BASE = "https://ecourts.gov.in/ecourt_mobile_app"

    # Headers mimicking the eCourts mobile app
    MOBILE_HEADERS = {
        "User-Agent": "eCourtsServices/3.0 (Android; API 30)",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Connection": "keep-alive",
    }

    def __init__(
        self,
        max_concurrent: int = 5,
        rate_limit_rpm: int = 30,
        use_mobile_fallback: bool = False,
    ):
        self.max_concurrent = max_concurrent
        self.rate_limit_rpm = rate_limit_rpm
        self.use_mobile_fallback = use_mobile_fallback
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers=self.MOBILE_HEADERS,
                follow_redirects=True,
                **proxy_for_httpx(),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # --- Primary fetch methods ---

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        retry=retry_if_not_exception_type(CaseNotFoundError),
        reraise=True,
    )
    async def fetch_case_by_cnr(self, cnr_number: str) -> dict:
        """
        Fetch full case details by CNR number.

        The CNR (Case Number Record) is a unique 16-character identifier
        assigned to every case filed in Indian courts.
        Format: SSDDCCCNNNNNNYYYY (State, District, Court, Serial, Year)

        Returns a normalized dict with:
        - case_type, case_number, filing_year, filing_date
        - court_name, court_complex, state, district
        - petitioner, respondent, advocates
        - status, next_hearing_date, stage, judge
        - acts_sections, orders list
        """
        async with self._semaphore:
            try:
                # Try bharat-courts SDK first (handles CAPTCHA automatically)
                data = await self._fetch_via_sdk(cnr_number)
            except CaseNotFoundError:
                raise
            except Exception as sdk_err:
                logger.warning(f"SDK fetch failed for {cnr_number}: {sdk_err}")
                if not self.use_mobile_fallback:
                    raise
                # Fallback to direct mobile API
                data = await self._fetch_via_mobile_api(cnr_number)

            # A portal can answer 200 with an empty page; don't treat that as a case
            if not any(data.get(k) for k in ("case_type", "petitioner", "respondent", "next_hearing_date", "status")):
                raise CaseNotFoundError(f"Case not found for CNR: {cnr_number}")
            return data

    async def _fetch_via_sdk(self, cnr_number: str) -> dict:
        """
        Use bharat-courts SDK for scraping.

        pip install bharat-courts[ocr]

        The SDK handles:
        - Session management with eCourts portals
        - CAPTCHA solving (OCR-based)
        - Parsing the HTML response into structured data
        """
        try:
            from bharat_courts import DistrictCourtClient, HCServicesClient, infer_court_from_cnr
            from bharat_courts.models import CourtType as SDKCourtType
        except ImportError:
            raise RuntimeError(
                "bharat-courts SDK not installed. "
                "Run: pip install bharat-courts[ocr]"
            )

        cnr = cnr_number.strip().upper()
        if len(cnr) != 16 or not cnr.isalnum():
            raise CaseNotFoundError(f"CNR must be 16 alphanumeric characters, got {cnr_number!r}")

        # CNR format: SSDDCCNNNNNNYYYY. The 4-letter prefix identifies the
        # issuing court; the SDK knows the prefixes of every High Court and
        # the Supreme Court, so anything it doesn't recognise is a district court.
        court = infer_court_from_cnr(cnr)
        if court is not None and court.court_type == SDKCourtType.SUPREME_COURT:
            raise CaseNotFoundError("Supreme Court CNR lookup is not supported yet")

        if court is not None and court.court_type == SDKCourtType.HIGH_COURT:
            async with HCServicesClient() as hc:
                detail = await hc.case_status_by_cnr(cnr)
            raw = self._case_detail_to_raw(detail)
            raw.setdefault("bench", court.bench)
            return self._normalize_case_data(raw, "high_court")

        async with DistrictCourtClient() as dc:
            detail = await dc.case_status_by_cnr(cnr)
        return self._normalize_case_data(self._case_detail_to_raw(detail), "district")

    @staticmethod
    def _case_detail_to_raw(detail) -> dict:
        """
        Flatten a bharat-courts CaseDetail into the key names
        _normalize_case_data() understands.
        """
        d = detail.to_dict()
        history = d.get("history") or []
        past_dates = [h["business_date"] for h in history if h.get("business_date")]
        orders = [
            {
                "date": o.get("order_date"),
                "type": o.get("order_type"),
                "judge": o.get("judge"),
                "link": o.get("pdf_url"),
                "description": o.get("order_type"),
            }
            for o in d.get("orders") or []
        ]
        reg_no = d.get("registration_number") or ""
        return {
            "cnr_number": d.get("cnr_number"),
            "case_type": d.get("case_type"),
            "case_number": reg_no or d.get("filing_number"),
            "filing_year": reg_no.rsplit("/", 1)[-1] if "/" in reg_no else None,
            "filing_date": d.get("filing_date"),
            "registration_date": d.get("registration_date"),
            "court_name": d.get("court_name"),
            "state": d.get("state"),
            "district": d.get("district"),
            "bench": d.get("bench_type"),
            "petitioner": _join(p.get("name") for p in d.get("petitioners") or []),
            "respondent": _join(p.get("name") for p in d.get("respondents") or []),
            "petitioner_advocate": _join(p.get("advocate") for p in d.get("petitioners") or []),
            "respondent_advocate": _join(p.get("advocate") for p in d.get("respondents") or []),
            "status": "Disposed" if d.get("decision_date") else (d.get("status") or "Pending"),
            "next_hearing_date": d.get("next_hearing_date"),
            "previous_hearing_date": max(past_dates) if past_dates else None,
            "stage": d.get("case_stage"),
            "judge": d.get("coram") or d.get("court_number_and_judge"),
            "acts_sections": [
                {"act": a.get("act"), "section": a.get("sections")} for a in d.get("acts") or []
            ],
            "orders": orders,
            "history": history,
        }

    async def _fetch_via_mobile_api(self, cnr_number: str) -> dict:
        """
        Direct call to eCourts mobile app API endpoints.
        These endpoints typically don't require CAPTCHA.
        """
        client = await self._get_client()

        # Mobile app endpoint for CNR-based search
        url = f"{self.MOBILE_API_BASE}/case_status_cnr"
        payload = {"cnr_number": cnr_number}

        resp = await client.post(url, data=payload)
        resp.raise_for_status()

        data = resp.json()
        return self._normalize_case_data(data, "district")

    async def fetch_cases_by_advocate(
        self,
        advocate_name: Optional[str],
        state: str,
        district: str = None,
        bar_code: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch all pending cases for an advocate by name or bar registration no.
        Useful for bulk onboarding — lawyer enters their name,
        we find all their cases.

        `state` is a High Court code ("delhi", "bombay"), court name, or state
        name. Only High Courts support advocate search on eCourts; `district`
        is accepted for API compatibility but unused.

        Raises ValueError if the state doesn't map to a High Court.
        """
        from bharat_courts import HCServicesClient

        court = resolve_high_court(state)
        if court is None:
            raise ValueError(f"No High Court found for {state!r}")
        try:
            async with self._semaphore:
                async with HCServicesClient() as hc:
                    results = await hc.case_status_by_advocate(
                        court,
                        advocate_name=None if bar_code else advocate_name,
                        bar_code=bar_code,
                        status_filter="Pending",  # only active cases
                    )
        except Exception as e:
            logger.error(f"Advocate search failed: {e}")
            return []

        # Results come back one row per party; keep one per CNR
        seen: dict[str, dict] = {}
        for r in results:
            if r.cnr_number and r.cnr_number not in seen:
                seen[r.cnr_number] = self._normalize_case_data(r.to_dict(), "high_court")
        return list(seen.values())

    async def fetch_cause_list(
        self, court_name: str, date_str: str, state: str
    ) -> list[dict]:
        """
        Fetch the cause list (daily board) for a specific court and date.
        Returns list of cases listed for hearing on that date.
        """
        try:
            from bharat_courts import DistrictCourts
            dc = DistrictCourts()
            results = await dc.cause_list(
                court=court_name,
                date=date_str,
                state=state,
            )
            return results or []
        except Exception as e:
            logger.error(f"Cause list fetch failed: {e}")
            return []

    # --- Normalization ---

    def _normalize_case_data(self, raw: dict, court_type: str) -> dict:
        """
        Normalize raw eCourts data into a standard schema
        regardless of source (district/HC/mobile API).
        """
        # The raw data structure varies by source, so we normalize
        normalized = {
            "cnr_number": self._extract(raw, ["cnr_number", "cnr", "CNRNumber"]),
            "case_type": self._extract(raw, ["case_type", "caseType", "type"]),
            "case_number": self._extract(raw, ["case_number", "caseNumber", "reg_no"]),
            "filing_year": self._extract_int(raw, ["filing_year", "filingYear", "year"]),
            "filing_date": self._extract_date(raw, ["filing_date", "filingDate", "dt_filing"]),
            "registration_date": self._extract_date(raw, ["registration_date", "regDate", "dt_registration"]),
            "court_type": court_type,
            "court_name": self._extract(raw, ["court_name", "courtName", "court"]),
            "court_complex": self._extract(raw, ["court_complex", "courtComplex", "complex_name"]),
            "state": self._extract(raw, ["state", "stateName", "state_name"]),
            "district": self._extract(raw, ["district", "districtName", "district_name"]),
            "bench": self._extract(raw, ["bench", "benchName"]),
            "petitioner": self._extract(raw, ["petitioner", "petitioners", "pet_name"]),
            "respondent": self._extract(raw, ["respondent", "respondents", "res_name"]),
            "petitioner_advocate": self._extract(raw, ["petitioner_advocate", "petAdv", "pet_adv"]),
            "respondent_advocate": self._extract(raw, ["respondent_advocate", "resAdv", "res_adv"]),
            "status": self._extract(raw, ["status", "caseStatus", "case_status"]),
            "next_hearing_date": self._extract_date(raw, ["next_hearing_date", "nextDate", "next_date", "dt_next_list"]),
            "previous_hearing_date": self._extract_date(raw, ["previous_hearing_date", "prevDate", "last_date"]),
            "stage": self._extract(raw, ["stage", "caseStage", "case_stage"]),
            "judge": self._extract(raw, ["judge", "judgeName", "judge_name"]),
            "acts_sections": self._extract_acts(raw),
            "orders": self._extract_orders(raw),
            "raw_data": raw,
        }
        normalized["data_hash"] = self._compute_hash(normalized)
        return normalized

    def _extract(self, data: dict, keys: list[str]) -> Optional[str]:
        for key in keys:
            val = data.get(key)
            if val:
                return str(val).strip()
        return None

    def _extract_int(self, data: dict, keys: list[str]) -> Optional[int]:
        val = self._extract(data, keys)
        if val:
            try:
                return int(val)
            except ValueError:
                return None
        return None

    def _extract_date(self, data: dict, keys: list[str]) -> Optional[str]:
        val = self._extract(data, keys)
        if not val:
            return None
        # Try common date formats
        for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"]:
            try:
                return datetime.strptime(val, fmt).date().isoformat()
            except ValueError:
                continue
        return val

    def _extract_acts(self, data: dict) -> list[dict]:
        acts = data.get("acts_sections") or data.get("acts") or data.get("act_section") or []
        if isinstance(acts, str):
            return [{"act": acts}]
        return acts if isinstance(acts, list) else []

    def _extract_orders(self, data: dict) -> list[dict]:
        orders = data.get("orders") or data.get("order_list") or data.get("orderList") or []
        if not isinstance(orders, list):
            return []
        return orders

    @staticmethod
    def _compute_hash(case_data: dict) -> str:
        """
        Compute a hash of the key fields for diff detection.
        When this changes between polls, we know the case was updated.
        """
        key_fields = {
            "next_hearing_date": case_data.get("next_hearing_date"),
            "status": case_data.get("status"),
            "stage": case_data.get("stage"),
            "judge": case_data.get("judge"),
            "orders_count": len(case_data.get("orders", [])),
        }
        return hashlib.sha256(
            json.dumps(key_fields, sort_keys=True, default=str).encode()
        ).hexdigest()


_STATE_TO_HC = {
    "maharashtra": "bombay", "goa": "bombay-goa", "west bengal": "calcutta",
    "andaman and nicobar": "calcutta", "uttar pradesh": "allahabad",
    "tamil nadu": "madras", "puducherry": "madras", "odisha": "orissa",
    "assam": "gauhati", "nagaland": "gauhati", "mizoram": "gauhati",
    "arunachal pradesh": "gauhati", "punjab": "punjab", "haryana": "punjab",
    "chandigarh": "punjab", "jammu and kashmir": "jammu", "ladakh": "jammu",
    "madhya pradesh": "mp", "himachal pradesh": "himachal", "andhra pradesh": "andhra",
    "bihar": "patna", "lakshadweep": "kerala",
}


def resolve_high_court(value: str):
    """Map a High Court code, court name, or state name to a bharat-courts Court."""
    from bharat_courts import get_court, get_court_by_name, list_high_courts

    key = (value or "").strip().lower().replace("&", "and")
    if not key:
        return None
    court = get_court(key) or get_court_by_name(value.strip())
    if court is None and key in _STATE_TO_HC:
        court = get_court(_STATE_TO_HC[key])
    if court is None:
        # "Delhi" -> "Delhi High Court", "Karnataka" -> "Karnataka High Court"
        for hc in list_high_courts():
            if not hc.bench and hc.name.lower().startswith(key):
                court = hc
                break
    return court


class CaseDiffDetector:
    """
    Compares old and new case data, identifies what changed,
    and determines which notifications to trigger.
    """

    @staticmethod
    def detect_changes(old_data: dict, new_data: dict) -> dict:
        """
        Returns a dict of changes: {field: {old: ..., new: ...}}
        """
        changes = {}
        watch_fields = [
            "next_hearing_date",
            "status",
            "stage",
            "judge",
            "bench",
        ]

        for field in watch_fields:
            old_val = old_data.get(field)
            new_val = new_data.get(field)
            if old_val != new_val and new_val is not None:
                changes[field] = {"old": old_val, "new": new_val}

        # Check for new orders
        old_orders = old_data.get("orders", [])
        new_orders = new_data.get("orders", [])
        if len(new_orders) > len(old_orders):
            new_order_count = len(new_orders) - len(old_orders)
            changes["new_orders"] = {
                "count": new_order_count,
                "orders": new_orders[-new_order_count:],
            }

        return changes

    @staticmethod
    def should_notify(changes: dict) -> list[str]:
        """
        Given a set of changes, determine which notification types to send.
        """
        notifications = []

        if "next_hearing_date" in changes:
            notifications.append("date_changed")

        if "new_orders" in changes:
            notifications.append("new_order")

        if "status" in changes:
            notifications.append("status_changed")

        if "judge" in changes or "bench" in changes:
            notifications.append("case_update")

        return notifications
