"""
What the district eCourts portal shows beyond bharat-courts' parse of a case page.

- Orders: the page's order links aren't URLs but displayPdf('…') calls whose
  arguments are only valid in the session that loaded the page. So a PDF is
  fetched by loading the case (one CAPTCHA) and asking for the PDF in that same
  session; we keep the order's number and date, never the arguments.
- Court reference: the hearing-history links (viewBusiness('2','9',…,'802',…))
  carry the establishment, district, state and court number, which is what the
  cause list and the judge's name are keyed on.
- Judge's name: the case page says "802-JUDICIAL MAGISTRATE FIRST CLASS - 11";
  the cause-list court dropdown says "802-Asha Rani-JUDICIAL MAGISTRATE…".
- Cause list: one court's list for one day, with each case's CNR, item number,
  purpose and the court's VC link.
"""
import html as html_lib
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("courtpilot.scraper.district")

PDF_ARGS = ("normal_v", "case_val", "court_code", "filename", "appFlag")
_ORDER_ROW = re.compile(
    r"<td>(?:&nbsp;|\s)*(\d+)\s*</td>\s*<td[^>]*>(?:&nbsp;|\s)*(\d{2}-\d{2}-\d{4}).*?</td>\s*<td[^>]*>(.*?)</td>",
    re.S | re.I)
_PDF_CALL = re.compile(r"displayPdf\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)")
_ARIA = re.compile(r"aria-label=\s*'([^']*)'")
_VIEW_BUSINESS = re.compile(
    r"viewBusiness\('([^']*)','([^']*)','[^']*','[A-Z0-9]{16}','([^']*)','[^']*','[^']*','([^']*)'")
_CL_ROW = re.compile(r"<tr>\s*<td>\s*(\d+)\s*</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", re.S | re.I)
_CL_HEADING = re.compile(r"<t[dh][^>]*colspan[^>]*>(.*?)</t[dh]>", re.S | re.I)
_CNR_ARG = re.compile(r"'([A-Z]{4}\d{12})'")


def _text(fragment: str) -> str:
    no_tags = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    no_tags = re.sub(r"<[^>]+>", " ", no_tags)
    lines = [re.sub(r"\s+", " ", html_lib.unescape(line)).strip() for line in no_tags.split("\n")]
    return "\n".join(line for line in lines if line)


def _iso(ddmmyyyy: str) -> Optional[str]:
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y").date().isoformat()
    except ValueError:
        return None


def parse_orders(page: str) -> list[dict]:
    """Orders on a district case page, oldest first: [{number, date, description, pdf_args}]."""
    start = page.find("order_table")
    if start < 0:
        return []
    end = page.find("</table>", start)
    out = []
    for number, when, cell in _ORDER_ROW.findall(page[start:end if end > 0 else None]):
        call = _PDF_CALL.search(cell)
        aria = _ARIA.search(cell)
        kind = (aria.group(1).split("|")[0].strip() if aria else "") or "Order"
        out.append({
            "number": number,
            "date": _iso(when),
            "description": kind.rstrip("s") if kind.endswith("Orders") else kind,  # "Interim Orders" -> "Interim Order"
            "pdf_args": list(call.groups()) if call else None,
        })
    return out


def stored_orders(page: str) -> list[dict]:
    """parse_orders() without the session-bound PDF arguments (what we save)."""
    return [{k: v for k, v in o.items() if k != "pdf_args"} for o in parse_orders(page)]


def parse_court_ref(page: str) -> Optional[dict]:
    """{"state_code", "dist_code", "est_code", "court_no"} of the court now hearing the case."""
    m = _VIEW_BUSINESS.search(page)  # first history row is the latest
    if not m:
        return None
    est, dist, state, court_no = m.groups()
    if not (est and dist and state and court_no):
        return None
    return {"state_code": state, "dist_code": dist, "est_code": est, "court_no": court_no}


def judge_from_court_option(option: str) -> str:
    """"802-Asha Rani-JUDICIAL MAGISTRATE FIRST CLASS - 11" -> "Asha Rani"."""
    parts = [p.strip() for p in (option or "").split("-")]
    if len(parts) < 3 or not parts[0].isdigit() or not parts[1]:
        return ""
    # Some courts list only a designation in the name slot
    if re.search(r"\b(JUDGE|MAGISTRATE|COURT|SESSIONS|TRIBUNAL|VACANT)\b", parts[1], re.I):
        return ""
    return parts[1]


def complex_for_est(complexes: dict[str, str], est_code: str) -> Optional[str]:
    """The complex value ("1260010@1,2,3,4@Y") whose establishments include est_code."""
    for value in complexes:
        bits = value.split("@")
        if len(bits) >= 2 and est_code in bits[1].split(","):
            return value
    return next(iter(complexes), None) if len(complexes) == 1 else None


def parse_cause_list(page: str) -> dict:
    """One court's cause list: {"judge", "vc_url", "entries": [{serial, cnr, case, parties, advocates, purpose, category}]}."""
    judge = re.search(r"In the court of(?:&nbsp;|\s)*:(?:&nbsp;|\s)*([^<]+)", page)
    vc = re.search(r"VC url\s*:\s*(https?://[^\s<'\"]+)", page, re.I)
    out = {"judge": html_lib.unescape(judge.group(1)).strip() if judge else "",
           "vc_url": vc.group(1) if vc else "", "entries": []}
    body = page[page.find("<tbody"):] if "<tbody" in page else page
    # Walk headings and rows in document order: headings set the category/purpose of the rows after them
    tokens = sorted([(m.start(), "h", m) for m in _CL_HEADING.finditer(body)] +
                    [(m.start(), "r", m) for m in _CL_ROW.finditer(body)], key=lambda t: t[0])
    category = purpose = ""
    for _, kind, m in tokens:
        if kind == "h":
            text = _text(m.group(1))
            if not text:
                continue
            if "case_type_lable" in m.group(0):
                category = text
            else:
                purpose = text
            continue
        serial, case_cell, parties, advocates = m.groups()
        cnr = _CNR_ARG.search(case_cell)
        case_text = _text(re.sub(r"<a[^>]*>.*?</a>", "", case_cell, flags=re.S))
        out["entries"].append({
            "serial": int(serial),
            "cnr": cnr.group(1) if cnr else "",
            "case": case_text.split("\n")[0],
            "parties": _text(parties).replace("\nversus\n", " vs ").replace("\n", " "),
            "advocates": _text(advocates).replace("\n", ", "),
            "purpose": purpose,
            "category": category,
        })
    return out


class DistrictPortal:
    """One eCourts district-portal session (goes through ECOURTS_PROXY_URL like every lookup)."""

    def __init__(self, client_factory=None):
        if client_factory is None:
            from bharat_courts import DistrictCourtClient

            client_factory = DistrictCourtClient
        self._factory = client_factory
        self._dc = None

    async def __aenter__(self):
        self._dc = self._factory()
        await self._dc.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._dc.__aexit__(*exc)

    async def case_page(self, cnr: str) -> str:
        """The full case page HTML for a CNR ("" if eCourts has no such case)."""
        from bharat_courts.districtcourts import endpoints

        last_error: Optional[Exception] = None
        for attempt in range(5):
            await self._dc._init_session()
            captcha = await self._dc._solve_captcha()
            if not captcha:
                continue
            try:
                result = await self._dc._post_ajax("cnr_status/searchByCNR",
                                                   endpoints.case_status_by_cnr_form(cnr=cnr, captcha=captcha))
            except Exception as e:  # wrong CAPTCHA reads come back as errors: new session, try again
                last_error = e
                continue
            page = result.get("casetype_list") or result.get("historytable") or ""
            if page:
                return page
            last_error = None
        if last_error:
            raise last_error
        return ""

    async def order_pdf(self, page: str, number: str) -> Optional[bytes]:
        """PDF of order `number` from a page this session just loaded."""
        from bharat_courts.districtcourts import endpoints

        order = next((o for o in parse_orders(page) if o["number"] == str(number)), None)
        if not order or not order["pdf_args"]:
            return None
        result = await self._dc._post_ajax("home/display_pdf", dict(zip(PDF_ARGS, order["pdf_args"])))
        path = str(result.get("order") or "")
        if not path or "://" in path or ".." in path:
            return None
        resp = await self._dc._http.get(f"{endpoints.BASE_URL}/{path.lstrip('/')}")
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            return None
        return resp.content

    async def complexes(self, state_code: str, dist_code: str) -> dict[str, str]:
        return await self._dc.list_complexes(state_code, dist_code)

    async def establishments(self, state_code: str, dist_code: str, complex_value: str) -> dict[str, str]:
        """Sections of a complex: {"2": "Chief Metropolitan Magistrate, West, THC", …}."""
        return await self._dc.list_establishments(state_code, dist_code, complex_value)

    async def court_options(self, state_code: str, dist_code: str, complex_value: str, est_code: str) -> dict[str, str]:
        """Cause-list courts: {"2^802": "802-Asha Rani-JUDICIAL MAGISTRATE FIRST CLASS - 11", …}."""
        return await self._dc.list_cause_list_courts(state_code, dist_code, complex_value, est_code)

    async def cause_list(self, ref: dict, complex_value: str, court_option: str, on: str, criminal: bool) -> str:
        """Raw cause list HTML for one court and date (DD-MM-YYYY)."""
        from bharat_courts.districtcourts import endpoints

        code = complex_value.split("@")[0]

        def build(captcha: str) -> dict:
            return endpoints.cause_list_form(
                state_code=ref["state_code"], dist_code=ref["dist_code"], court_complex_code=code,
                est_code=ref["est_code"], court_no=f"{ref['est_code']}^{ref['court_no']}", court_name=court_option,
                causelist_date=on, civil=not criminal, captcha=captcha)

        result = await self._dc._post_with_captcha_retry(
            "cause_list/submitCauseList", build, state_code=ref["state_code"], dist_code=ref["dist_code"],
            court_complex_code=code, est_code=ref["est_code"])
        return result.get("case_data", "") or ""
