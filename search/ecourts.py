"""
District court searches on the eCourts portal (services.ecourts.gov.in/ecourtindia_v6).

bharat-courts covers party and case-number search; FIR and advocate search,
police stations and the dropdown cascade are done here with the SDK's
session/CAPTCHA plumbing (its underscore methods), so if the portal or SDK
changes, this is the one file to fix.

Every search is scoped to one court complex (and, where the portal insists,
one registration year), takes one CAPTCHA, and returns the same results table,
parsed by parse_results().
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from bs4 import BeautifulSoup
from bharat_courts.districtcourts.client import DistrictCourtClient
from bharat_courts.districtcourts.parser import parse_option_tags

logger = logging.getLogger("courtpilot.search.ecourts")

CNR_IN_TEXT = re.compile(r"'([A-Z]{4}[0-9A-Z]{12})'")
CASE_NO_RE = re.compile(r"^[^/]+/\d+/\d{4}$")
FIR_RE = re.compile(r"^\d+/\d{4}$")


@dataclass(frozen=True)
class Court:
    """A court complex as the portal identifies it."""
    state_code: str
    dist_code: str
    complex_value: str  # "1170073@6,22,28@N"
    name: str = ""

    @property
    def complex_code(self) -> str:
        return self.complex_value.split("@")[0]

    @property
    def establishments(self) -> list[str]:
        """Establishment codes to search when the portal needs one picked (flag Y)."""
        parts = self.complex_value.split("@")
        if len(parts) > 2 and parts[2] == "Y" and parts[1]:
            return parts[1].split(",")
        return [""]


@dataclass
class Hit:
    cnr_number: str
    case_number: str = ""
    case_type: str = ""
    reg_year: Optional[int] = None
    petitioner: str = ""
    respondent: str = ""
    fir: str = ""
    court_name: str = ""
    extra: dict = field(default_factory=dict)


class SearchUnavailable(Exception):
    """eCourts didn't answer (CAPTCHA kept failing, timeout, portal error)."""


def parse_results(html: str) -> list[Hit]:
    """
    The portal's results table for party / FIR / advocate / case-number
    searches. Columns vary by search type, so cells are recognised by shape:
    the CNR sits in the View link's onClick, "TYPE/NO/YEAR" is the case
    number, "NO/YEAR" the FIR, the cell with "Vs" the parties. Rows grouped
    under a court heading get that court's name. One row per CNR (party
    search repeats a case once per matching party).
    """
    if not html or "<table" not in html:
        return []
    soup = BeautifulSoup(html, "lxml")
    hits: dict[str, Hit] = {}
    court = ""
    for row in soup.find_all("tr"):
        heading = row.find("th", attrs={"colspan": True})
        if heading is not None:
            court = heading.get_text(" ", strip=True)
            continue
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        m = CNR_IN_TEXT.search(str(row))
        if not m:
            continue
        cnr = m.group(1)
        hit = hits.get(cnr) or Hit(cnr_number=cnr, court_name=court)
        parties_seen = False  # cells after the parties in a row are advocates
        for cell in cells[1:]:
            text = cell.get_text(" ", strip=True)
            if not text or text.lower() == "view":
                continue
            if CASE_NO_RE.match(text):
                if not hit.case_number:
                    hit.case_number = text
                    hit.case_type = text.split("/")[0].strip()
                    hit.reg_year = int(text.rsplit("/", 1)[1])
            elif FIR_RE.match(text):
                hit.fir = text
            else:
                # Parties are "A<br>Vs</br>B"; the parser turns the stray </br>
                # into nothing, leaving "A<br/>VsB"
                inner = cell.decode_contents()
                halves = re.split(r"<br\s*/?>\s*Vs\.?\s*(?:</?br\s*/?>)?", inner, maxsplit=1, flags=re.I)
                pet = BeautifulSoup(halves[0], "lxml").get_text(" ", strip=True)
                res = BeautifulSoup(halves[1], "lxml").get_text(" ", strip=True) if len(halves) > 1 else ""
                if parties_seen:
                    advocates = [a.strip() for a in re.split(r"<br\s*/?>", inner) if a.strip()]
                    hit.extra["advocates"] = [BeautifulSoup(a, "lxml").get_text(" ", strip=True) for a in advocates]
                elif not hit.petitioner:
                    hit.petitioner, hit.respondent = pet, res
                elif pet not in (hit.petitioner, hit.respondent):
                    # Party search lists each matching party on its own row: keep them all
                    hit.extra.setdefault("other_parties", []).append(pet)
                parties_seen = True
        hits[cnr] = hit
    return list(hits.values())


def _options(html: str) -> dict[str, str]:
    """<option> tags, tolerating the portal's unquoted values."""
    out = {}
    for m in re.finditer(r"<option[^>]*value=['\"]?([^'\" >]*)['\"]?[^>]*>([^<]*)", html or ""):
        value, label = m.group(1).strip(), m.group(2).strip()
        if value and value != "0" and "select" not in label.lower():
            out[value] = label
    return out


class DistrictSearch:
    """Thin async wrapper; one SDK client (and portal session) per call."""

    def __init__(self, client_factory: Callable[[], DistrictCourtClient] = DistrictCourtClient):
        self._client_factory = client_factory

    async def _post(self, court: Court, action: str, est: str, form: Callable[[str], dict], key: str) -> list[Hit]:
        client = self._client_factory()
        try:
            async with client:
                result = await client._post_with_captcha_retry(
                    action, form, state_code=court.state_code, dist_code=court.dist_code,
                    court_complex_code=court.complex_code, est_code=est,
                )
        except Exception as e:
            raise SearchUnavailable(str(e)) from e
        hits = parse_results(result.get(key, ""))
        for h in hits:
            h.court_name = h.court_name or court.name
        return hits

    def _base(self, court: Court, est: str) -> dict:
        return {"state_code": court.state_code, "dist_code": court.dist_code,
                "court_complex_code": court.complex_code, "est_code": est}

    # --- Searches ---

    async def party(self, court: Court, name: str, year: int, status: str = "Both") -> list[Hit]:
        out: list[Hit] = []
        for est in court.establishments:
            out += await self._post(court, "casestatus/submitPartyName", est, lambda cap: {
                "petres_name": name, "rgyearP": str(year), "case_status": status, "fcaptcha_code": cap,
                **self._base(court, est),
            }, "party_data")
        return out

    async def case_number(self, court: Court, case_type: str, number: str, year: int) -> list[Hit]:
        est = case_type.split("^")[1] if "^" in case_type else court.establishments[0]
        return await self._post(court, "casestatus/submitCaseNo", est, lambda cap: {
            "case_type": case_type, "search_case_no": number, "case_no": number, "rgyear": str(year),
            "case_captcha_code": cap, **self._base(court, est),
        }, "case_data")

    async def fir(self, court: Court, police_station: str, fir_no: str, year: int, status: str = "Both") -> list[Hit]:
        code, _, uniform = police_station.partition("-")
        out: list[Hit] = []
        for est in court.establishments:
            out += await self._post(court, "casestatus/submitFirNo", est, lambda cap: {
                "police_st_code": code, "uniform_code": uniform, "fir_no": fir_no, "firyear": str(year),
                "case_status": status, "fir_captcha_code": cap, **self._base(court, est),
            }, "case_data")
        return out

    async def advocate(self, court: Court, *, name: str = "", bar_state: str = "", bar_code: str = "",
                       bar_year: str = "", status: str = "Pending") -> list[Hit]:
        by_bar = bool(bar_code)
        out: list[Hit] = []
        for est in court.establishments:
            out += await self._post(court, "casestatus/submitAdvName", est, lambda cap: {
                "radAdvt": "2" if by_bar else "1", "advocate_name": "" if by_bar else name,
                "adv_bar_state": bar_state, "adv_bar_code": bar_code, "adv_bar_year": bar_year,
                "case_status": status, "caselist_date": "", "adv_captcha_code": cap, "case_type": "",
                **self._base(court, est),
            }, "adv_data")
        return out

    # --- Dropdowns (no CAPTCHA) ---

    async def states(self) -> dict[str, str]:
        async with self._client_factory() as c:
            return await c.list_states()

    async def districts(self, state_code: str) -> dict[str, str]:
        async with self._client_factory() as c:
            return await c.list_districts(state_code)

    async def complexes(self, state_code: str, dist_code: str) -> dict[str, str]:
        async with self._client_factory() as c:
            return await c.list_complexes(state_code, dist_code)

    async def case_types(self, court: Court) -> dict[str, str]:
        async with self._client_factory() as c:
            return await c.list_case_types(court.state_code, court.dist_code, court.complex_code,
                                           court.establishments[0])

    async def police_stations(self, court: Court) -> dict[str, str]:
        """{"20501-11213010": "DHORAJI POLICE STATION - RAJKOT DISTRICT 20501", ...} (state-wide)."""
        async with self._client_factory() as c:
            await c._init_session()
            await c._setup_court(state_code=court.state_code, dist_code=court.dist_code,
                                 court_complex_code=court.complex_code, est_code=court.establishments[0])
            result = await c._post_ajax("casestatus/fillPoliceStation", self._base(court, court.establishments[0]))
            return _options(result.get("police_station_list", ""))


__all__ = ["Court", "Hit", "DistrictSearch", "SearchUnavailable", "parse_results", "parse_option_tags"]
