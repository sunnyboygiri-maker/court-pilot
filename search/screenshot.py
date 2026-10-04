"""
Read case details out of screenshots and photos, with free local OCR.

RapidOCR (open source, runs on our own server, no per-image cost) turns the
image into text boxes; the rules below turn those boxes into cases:

1. Any CNR visible anywhere (case status pages, orders) is used as is.
2. The eCourts app's "My Cases" screen: a grey court header such as
   "Chief Metropolitan Magistrate, West, THC,West (1)" (which is the portal's
   own name for that court section), then a table with Next date | Case
   Number | Party Name columns. Values don't line up exactly with their
   headings and long case numbers wrap ("Ct. Cases/49" + "90319/2016"), so
   columns are split halfway between headings and wrapped numbers re-joined.
3. The eCourts app's Case History screen: label | value rows ("Registration
   Number | Cr. Case/71234/2016", "CNR Number | …", "Next Hearing Date | …").
   The case number searched on is the registration number, never the
   filing number shown above it.
4. Otherwise, the first "TYPE/NUMBER/YEAR" and "A Vs B" in the text.
"""
import re
import threading
from dataclasses import asdict, dataclass
from typing import Optional

from scraper.persist import normalize_cnr

MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
CASE_RE = re.compile(r"([A-Za-z][A-Za-z .()\-]*?)\s*/\s*(\d{1,8})\s*/\s*((?:19|20)\d{2})")
CNR_RE = re.compile(r"\b([A-Z]{4}[0-9O]{2}[0-9O]{6}(?:19|20)[0-9]{2})\b")
UI_WORDS = {"home", "case status", "cause list", "my cases", "calendar", "date wise", "district wise",
            "district court", "enter text to search", "next/", "disp", "osal", "date", "doto", "dato"}

_engine = None
_engine_lock = threading.Lock()


@dataclass
class ReadCase:
    """What the screenshot says about one case; any field may be missing."""
    cnr: str = ""
    court_header: str = ""
    case_type: str = ""
    number: str = ""
    year: str = ""
    petitioner: str = ""
    respondent: str = ""
    next_date: str = ""
    status: str = ""

    def label(self) -> str:
        if self.number:
            return f"{self.case_type} {self.number}/{self.year}".strip()
        return self.cnr or "Case"

    def to_dict(self) -> dict:
        return asdict(self)


def ocr(image: bytes) -> list[tuple[float, float, str]]:
    """Text boxes as (left x, top y, text). CPU-bound: call from a thread."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR

            _engine = RapidOCR()
        result = _engine(image)
    if not result.txts:
        return []
    return [(float(b[0][0]), float(b[0][1]), t.strip()) for b, t in zip(result.boxes, result.txts) if t.strip()]


def _find(items, label: str):
    return [(x, y) for x, y, t in items if t.lower() == label]


def _court_header(items, top: float, bottom: float) -> str:
    lines = [t for x, y, t in sorted(items, key=lambda i: (i[1], i[0]))
             if top < y < bottom and t.lower() not in UI_WORDS and not re.fullmatch(r"[\W\d]+|[^\x00-\x7F]+", t)]
    return re.sub(r"\s*\(\d+\)\s*$", "", " ".join(lines)).strip(" ,")


def _join_case_text(parts: list[str]) -> str:
    out = ""
    for t in parts:
        if out and (out[-1].isdigit() or out[-1] == "/") and t[:1].isdigit():
            out += t  # "Ct. Cases/49" + "90319/2016"
        elif out.endswith("/"):
            out += t
        else:
            out += " " + t
    return re.sub(r"^\s*\d+\)\s*", "", out.strip())


def _case_fields(text: str) -> tuple[str, str, str]:
    m = CASE_RE.search(text)
    if not m:
        return "", "", ""
    case_type = re.sub(r"^\d+\)\s*", "", m.group(1)).strip(" .")
    return case_type, m.group(2), m.group(3)


def _parties(text: str) -> tuple[str, str]:
    parts = re.split(r"\s+Vs\.?\s+|\s+V/s\.?\s+|\s+Versus\s+", " " + text.strip() + " ", maxsplit=1, flags=re.I)
    pet = parts[0].strip()
    res = parts[1].strip() if len(parts) > 1 else ""
    return pet, res


def _my_cases_screen(items) -> list[ReadCase]:
    """The eCourts app's My Cases list (one or more court groups, one or more cases each)."""
    case_heads = sorted(_find(items, "case number"), key=lambda p: p[1])
    party_heads = sorted(_find(items, "party name"), key=lambda p: p[1])
    if not case_heads or not party_heads:
        return []
    found: list[ReadCase] = []
    search_box = max((y for x, y, t in items if "search" in t.lower()), default=0)
    prev_bottom = search_box
    for i, (case_x, head_y) in enumerate(case_heads):
        party_x = min(party_heads, key=lambda p: abs(p[1] - head_y))[0]
        next_heads = [y for x, y, t in items if t.lower() == "next/" and abs(y - head_y) < 120]
        table_top = min(next_heads + [head_y])
        date_x = min((x for x, y, t in items if t.lower() == "next/" and abs(y - head_y) < 120), default=0)
        region_end = case_heads[i + 1][1] - 250 if i + 1 < len(case_heads) else float("inf")
        header = _court_header(items, prev_bottom + 5, table_top - 15)
        edge1, edge2 = (date_x + case_x) / 2, (case_x + party_x) / 2

        rows = [(x, y, t) for x, y, t in items
                if head_y + 60 < y < region_end and t.lower() not in UI_WORDS and not re.fullmatch(r"\d|[^\x00-\x7F]+", t)]
        # A case starts at each month in the date column ("Feb" / "04" / "2026")
        starts = sorted(y for x, y, t in rows if x < edge1 and t[:3] in MONTHS)
        for j, start in enumerate(starts):
            stop = starts[j + 1] - 15 if j + 1 < len(starts) else region_end
            cell = {"date": [], "case": [], "party": []}
            for x, y, t in sorted(rows, key=lambda r: (r[1], r[0])):
                if start - 40 <= y < stop:
                    cell["date" if x < edge1 else ("case" if x < edge2 else "party")].append(t)
            case_type, number, year = _case_fields(_join_case_text(cell["case"]))
            pet, res = _parties(" ".join(cell["party"]))
            date_bits = [t for t in cell["date"] if t[:3] in MONTHS or re.fullmatch(r"\d{1,4}", t)][:3]
            status = next((t for t in cell["date"] if t.lower() in ("pending", "disposed")), "")
            if number or pet:
                found.append(ReadCase(court_header=header, case_type=case_type, number=number, year=year,
                                      petitioner=pet, respondent=res, next_date=" ".join(date_bits), status=status))
        prev_bottom = region_end
    return found


LABEL_STARTS = ("filing", "registration", "cnr", "first hearing", "next hearing", "case stage", "case status",
                "court number", "decision", "nature of disposal", "under act", "under section", "first", "next",
                "court", "case")
DATE_RE = re.compile(r"\b(\d{1,2})[-./](\d{1,2})[-./]((?:19|20)\d{2})\b")


def _labelled_rows(items) -> dict[str, str]:
    """Case History / Case Details screens: {"registration number": "Cr. Case/71234/2016", ...}."""
    labels = [(x, y, t) for x, y, t in items if t.lower().startswith(LABEL_STARTS) and not re.search(r"\d{3}", t)]
    if len(labels) < 3:
        return {}
    label_x = min(x for x, _, _ in labels)
    # Values sit well to the right of the label column; long labels wrap onto a second line
    values = [(x, y, t) for x, y, t in items if x > label_x + 120]
    left = [(x, y, t) for x, y, t in items if x <= label_x + 120]
    rows: dict[str, str] = {}
    for vx, vy, vt in sorted(values, key=lambda v: v[1]):
        near = sorted((ly, lt) for lx, ly, lt in left if vy - 30 <= ly <= vy + 55)
        if not near:
            continue
        label = " ".join(lt for _, lt in near).lower()
        label = re.sub(r"\s+", " ", label)
        if label in rows:
            rows[label] += " " + vt  # value wrapped onto a second line
        else:
            rows[label] = vt
    return rows


def _row(rows: dict[str, str], *starts: str) -> str:
    for label, value in rows.items():
        if any(label.startswith(s) for s in starts):
            return value
    return ""


def _details_screen(items) -> list[ReadCase]:
    rows = _labelled_rows(items)
    if not rows:
        return []
    reg = _row(rows, "registration number", "registration no")
    case_type, number, year = _case_fields(re.sub(r"\s*/\s*", "/", reg))
    cnr_text = _row(rows, "cnr")
    m = CNR_RE.search(cnr_text.upper().replace(" ", ""))
    cnr = normalize_cnr(m.group(1)[:4] + m.group(1)[4:].replace("O", "0")) if m else ""
    nxt = DATE_RE.search(_row(rows, "next hearing", "next date"))
    if not (number or cnr):
        return []
    return [ReadCase(cnr=cnr or "", case_type=case_type, number=number, year=year,
                     next_date=f"{nxt.group(1)}-{nxt.group(2)}-{nxt.group(3)}" if nxt else "",
                     status=_row(rows, "case status"),
                     court_header="")]


def read_cases(items: list[tuple[float, float, str]]) -> list[ReadCase]:
    """All the cases a screenshot shows."""
    details = _details_screen(items)
    if details:
        return details
    text = " ".join(t for x, y, t in sorted(items, key=lambda i: (i[1], i[0])))
    cases: list[ReadCase] = []
    for m in CNR_RE.finditer(text.upper()):
        cnr = normalize_cnr(m.group(1).replace("O", "0")[:4] + m.group(1)[4:].replace("O", "0"))
        if cnr and all(c.cnr != cnr for c in cases):
            cases.append(ReadCase(cnr=cnr))
    cases += _my_cases_screen(items)
    if not cases:
        # Skip a filing number ("Filing Number Cr. Case/130001/2016"): eCourts searches by registration number
        text_for_number = re.sub(r"filing\s*(?:number|no\.?)\s*[A-Za-z][A-Za-z .()\-]*?/\s*\d+\s*/\s*\d{4}", " ",
                                 text, flags=re.I)
        case_type, number, year = _case_fields(re.sub(r"\s*/\s*", "/", text_for_number))
        vs = re.search(r"([A-Z][A-Z .&]{2,}?)\s+Vs\.?\s+([A-Z][A-Z .&]{2,})", text)
        if number or vs:
            cases.append(ReadCase(case_type=case_type, number=number, year=year,
                                  petitioner=vs.group(1).strip() if vs else "",
                                  respondent=vs.group(2).strip() if vs else ""))
    return cases


def read_image(image: bytes) -> list[ReadCase]:
    return read_cases(ocr(image))
