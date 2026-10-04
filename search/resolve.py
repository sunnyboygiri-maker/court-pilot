"""
Turn what a screenshot says (search.screenshot.ReadCase) into the exact eCourts case.

The eCourts app's court header ("Chief Metropolitan Magistrate, West, THC")
is the portal's own name for a court section, so it's matched against a
directory of every section in the lawyer's states (built once from the
portal's dropdowns, cached a week). Then the section's case types are matched
to the screenshot's ("Cr. Case" -> "Cr. Case - CRIMINAL CASE") and the case is
looked up by number, the same exact search a lawyer would do by hand. The
parties on the screenshot must match the result's.
"""
import json
import logging
import re
from typing import Optional

from search import names
from search.ecourts import Court, DistrictSearch, Hit
from search.screenshot import ReadCase

logger = logging.getLogger("courtpilot.search.resolve")

CACHE_TTL = 7 * 24 * 3600
MAX_SECTIONS_TRIED = 3
MAX_FALLBACK_SECTIONS = 20


async def _cached(redis, key: str, fetch):
    hit = redis.get(key)
    if hit:
        return json.loads(hit)
    data = await fetch()
    if data:
        redis.set(key, json.dumps(data), ex=CACHE_TTL)
    return data


async def districts_of(engine: DistrictSearch, redis, state_code: str) -> dict[str, str]:
    return await _cached(redis, f"dc:districts:{state_code}", lambda: engine.districts(state_code)) or {}


async def district_sections(engine: DistrictSearch, redis, state_code: str, dist_code: str, dist_name: str) -> list[dict]:
    """Every court section of one district: [{"court": {...}, "est": "2", "name": "Chief Metropolitan Magistrate, West, THC, West"}]."""
    async def build():
        out = []
        complexes = await engine.complexes(state_code, dist_code)
        for value, cx_name in complexes.items():
            court = Court(state_code, dist_code, value, cx_name.title())
            split = court.establishments != [""]
            try:
                sections = await engine.establishments(Court(state_code, dist_code, value.replace("@N", "@Y"), cx_name))
            except Exception:
                sections = {}
            court_d = {"state_code": state_code, "dist_code": dist_code, "complex_value": value,
                       "name": f"{cx_name.title()}, {dist_name.title()}"}
            for est, est_name in (sections or {"": cx_name}).items():
                out.append({"court": court_d, "est": est if split else "", "name": f"{est_name}, {dist_name}"})
        return out

    return await _cached(redis, f"dc:sections:{state_code}:{dist_code}", build) or []


def header_districts(header: str, districts: dict[str, str]) -> list[str]:
    """
    The eCourts app ends its court header with the district: "...THC,West",
    "...KKD,East", "...DWK,South West". Codes of the districts it names, best first.
    """
    tail = _words(header.rsplit(",", 1)[-1]) if "," in header else _words(header)
    exact = [code for code, name in districts.items() if _words(name) == tail]
    if exact:
        return exact
    partial = [(len(_words(name) & tail) / len(_words(name)), code) for code, name in districts.items() if _words(name) & tail]
    return [code for score, code in sorted(partial, reverse=True) if score >= 0.5][:2]


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w}


def header_score(header: str, section_name: str) -> float:
    """Share of the section's words that the screenshot's header contains."""
    want = _words(section_name)
    return len(want & _words(header)) / len(want) if want else 0.0


def _norm_type(text: str) -> str:
    return re.sub(r"[^A-Z]", "", (text or "").upper())


def best_case_type(types: dict[str, str], wanted: str) -> list[str]:
    """Case type codes whose short name matches the screenshot's ("Cr. Case" ~ "Cr. Case - CRIMINAL CASE")."""
    w = _norm_type(wanted)
    if not w:
        return []
    short = {code: _norm_type(label.split(" - ")[0]) for code, label in types.items()}
    exact = [c for c, s in short.items() if s == w]
    if exact:
        return exact
    return [c for c, s in short.items() if s.startswith(w) or w.startswith(s)][:3]


def _generic(party: str) -> bool:
    """"STATE", "Union of India": on one side of countless cases, so proves nothing."""
    words = names.tokens(party)
    return not words or all(w in names.COMMON for w in words)


def parties_score(read: ReadCase, hit: Hit) -> Optional[float]:
    """How well the screenshot's parties match the case found (None if the screenshot shows none)."""
    sides = [p for p in (read.petitioner, read.respondent) if p and not _generic(p)]
    if not sides:
        return None
    scores = [max(names.name_score(p, hit.petitioner), names.name_score(p, hit.respondent)) for p in sides]
    return sum(scores) / len(scores)


async def lawyer_sections(engine, redis, places: list[tuple[str, str]]) -> list[dict]:
    """Sections of the districts the lawyer practises in: places are (state code, district code or name)."""
    out = []
    for state, dist in places:
        districts = await districts_of(engine, redis, state)
        code = dist if dist in districts else next((c for c, n in districts.items() if n.lower() == str(dist).lower()), None)
        if code:
            out += await district_sections(engine, redis, state, code, districts[code])
    return out


async def candidate_sections(engine, redis, read: ReadCase, state_codes: list[str], my_courts: list[dict]) -> list[dict]:
    if read.court_header:
        ranked = []
        for state in state_codes:
            try:
                districts = await districts_of(engine, redis, state)
                for dist in header_districts(read.court_header, districts):
                    for sec in await district_sections(engine, redis, state, dist, districts[dist]):
                        score = header_score(read.court_header, sec["name"])
                        if score >= 0.6:
                            ranked.append((score, sec))
            except Exception:
                logger.exception("Couldn't list courts for state %s", state)
        ranked.sort(key=lambda r: -r[0])
        if ranked:
            return [sec for _, sec in ranked[:MAX_SECTIONS_TRIED]]
    # No (usable) header: try the lawyer's own courts, section by section
    out = []
    for c in my_courts:
        court = Court(c["state_code"], c["dist_code"], c["complex_value"], c.get("name", ""))
        for est in court.establishments:
            out.append({"court": c, "est": est, "name": c.get("name", "")})
    return out[:6]


async def resolve(engine: DistrictSearch, redis, read: ReadCase, state_codes: list[str],
                  my_courts: list[dict], places: Optional[list[tuple[str, str]]] = None) -> tuple[list[Hit], Optional[dict]]:
    """
    Every eCourts case the screenshot could be, and the court section of the first.

    All candidate sections are tried, not just until the first match: the same
    type/number/year exists in several courts, and one match found early isn't
    proof there isn't another. Each hit carries its section in extra["section"].
    """
    if read.cnr:
        return [Hit(cnr_number=read.cnr, case_type=read.case_type, case_number=read.label() if read.number else "",
                    petitioner=read.petitioner, respondent=read.respondent)], None
    if not (read.number and read.year and read.case_type):
        return [], None
    tried: set[tuple] = set()
    found: dict[str, Hit] = {}
    first: Optional[dict] = None

    async def try_section(sec: dict, exact_only: bool = False) -> list[Hit]:
        c = sec["court"]
        key = (c["complex_value"], sec["est"])
        if key in tried:
            return []
        tried.add(key)
        court = Court(c["state_code"], c["dist_code"], c["complex_value"], c.get("name", ""))
        est = sec["est"] or None
        types = await _cached(redis, f"dc:casetypes:{c['state_code']}:{c['dist_code']}:{c['complex_value']}:{sec['est']}",
                              lambda: engine.case_types(court, est))
        codes = best_case_type(types or {}, read.case_type)
        if exact_only:
            codes = [k for k in codes if _norm_type((types or {})[k].split(" - ")[0]) == _norm_type(read.case_type)]
        for code in codes:
            hits = await engine.case_number(court, code, read.number, int(read.year))
            for h in hits:
                h.court_name = h.court_name or sec["name"]
                h.extra["section"] = sec
            good = [h for h in hits if (parties_score(read, h) or 1.0) >= 0.6]
            if good:
                return good
        return []

    async def collect(sec: dict, exact_only: bool = False) -> None:
        nonlocal first
        for h in await try_section(sec, exact_only):
            found.setdefault(h.cnr_number, h)
            first = first or sec

    for sec in await candidate_sections(engine, redis, read, state_codes, my_courts):
        await collect(sec)
    if not found and not read.court_header and places:
        # Court not on the screenshot: every section of the lawyer's districts
        # that has exactly this case type (e.g. "CS DJ ADJ" exists only at District Judge level)
        for sec in (await lawyer_sections(engine, redis, places))[:MAX_FALLBACK_SECTIONS]:
            try:
                await collect(sec, exact_only=True)
            except Exception:
                logger.warning("Lookup failed in %s", sec.get("name"))
    return list(found.values()), first


# --- Checking a candidate against its full eCourts record ---

def _read_date(text: str):
    """"27-10-2026" or the My Cases column's "Oct 27 2026" -> date."""
    from datetime import date

    from search.screenshot import DATE_RE, MONTHS

    m = DATE_RE.search(text or "")
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        bits = (text or "").split()
        month = next((MONTHS[b[:3].title()] for b in bits if b[:3].title() in MONTHS), None)
        nums = [int(b) for b in bits if b.isdigit()]
        year = next((n for n in nums if n > 1900), None)
        day = next((n for n in nums if n <= 31), None)
        if month and year and day:
            return date(year, month, day)
    except ValueError:
        pass
    return None


def _number_year(case_number: str) -> tuple[str, str]:
    nums = re.findall(r"\d+", case_number or "")
    return (nums[-2].lstrip("0"), nums[-1]) if len(nums) >= 2 else ("", "")


def verify(read: ReadCase, data: dict) -> dict:
    """
    Compare what the screenshot says with the case's full record.

    Each check is True (agrees), False (contradicts) or absent (nothing to
    compare). Sure = no contradiction and at least one independent agreement
    (a CNR on the screenshot counts as one). A misread digit lands on a sibling
    case with the same parties, so parties alone only count when they aren't
    "State" and the hearing dates don't disagree.
    """
    from scraper.persist import parse_date

    checks: dict[str, bool] = {}
    if read.number and read.year:
        num, year = _number_year(data.get("case_number") or "")
        if num:
            checks["number"] = (num, year) == (read.number.lstrip("0"), read.year)
    seen_date = _read_date(read.next_date)
    if seen_date:
        raw = data.get("raw_data") or {}
        known = {parse_date(data.get("next_hearing_date"))}
        for h in raw.get("history") or []:
            known |= {parse_date(h.get("hearing_date")), parse_date(h.get("business_date"))}
        checks["date"] = seen_date in known
    sides = [p for p in (read.petitioner, read.respondent) if p and not _generic(p)]
    if sides:
        score = parties_score(read, Hit(cnr_number="", petitioner=data.get("petitioner") or "",
                                        respondent=data.get("respondent") or ""))
        if score is not None and score >= 0.88:
            checks["parties"] = True
        elif score is not None and score < 0.6:
            checks["parties"] = False
    contradicted = any(v is False for v in checks.values())
    if read.cnr:
        sure = not contradicted
    else:
        sure = not contradicted and (checks.get("date") is True or checks.get("parties") is True)
    return {
        "sure": sure,
        "checks": checks,
        "next_date": data.get("next_hearing_date"),
        "stage": data.get("stage"),
        "court": data.get("court_name"),
        "judge": data.get("judge"),
        "petitioner": data.get("petitioner"),
        "respondent": data.get("respondent"),
        "case_number": data.get("case_number"),
        "case_type": data.get("case_type"),
    }
