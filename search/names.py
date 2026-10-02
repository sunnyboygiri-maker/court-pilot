"""
Indian-name matching for case search.

eCourts party search is a case-insensitive *substring* match on the exact
letters typed: "KANGD" finds KANGDA, but "KANGADA" finds nothing, and
Jitender / Jitendar / Jitendra / Jeetender are four different strings to it.

Two jobs here:

1. search_stems(): from what the lawyer typed, the few short literal strings
   to send to eCourts so that every common spelling of the name contains one
   of them (cut the variable endings, branch on variable middles).
2. name_score(): rank what eCourts returns against what was typed, using a
   phonetic "skeleton" so spelling variants score as matches.
"""
import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

# Words that carry no identity: titles, relations, company suffixes
NOISE = {
    "SHRI", "SHREE", "SRI", "SH", "SMT", "SMT.", "KUMARI", "KU", "MR", "MRS", "MS", "MISS", "DR", "ADV",
    "ADVOCATE", "LATE", "MASTER", "BABY", "SO", "DO", "WO", "CO", "AND", "ORS", "OTHERS", "ANR", "ANOTHER",
    "THE", "OF", "VS", "VERSUS", "THROUGH", "THRU", "BY", "ALIAS", "URF", "MS.", "M/S", "PVT", "LTD", "LIMITED",
    "PRIVATE", "LLP", "INC", "CO.",
}
# Very common name parts: fine for ranking, useless as a search term on their own
COMMON = {
    "KUMAR", "KUMARI", "SINGH", "DEVI", "BAI", "BEN", "BHAI", "LAL", "CHAND", "PRASAD", "RAM", "DAS", "NATH",
    "KAUR", "BEGUM", "BANO", "KHAN", "SHAH", "PATEL", "SHARMA", "VERMA", "GUPTA", "YADAV", "STATE", "GOVT",
    "GOVERNMENT", "INDIA", "UNION", "POLICE", "STATION", "MOHD", "MD", "SAHEB", "SAHIB", "JI", "BABU", "RANI",
}

# Name families spelled very differently; searched by each listed literal
FAMILIES = [
    {"MOHAMMAD", "MOHAMMED", "MOHAMAD", "MOHAMED", "MUHAMMAD", "MUHAMMED", "MOHD", "MD", "MOHAMMAD"},
    {"CHAUDHARY", "CHAUDHRY", "CHOUDHARY", "CHOUDHRY", "CHOWDHURY", "CHAUDHARI", "CHOUDHARI", "CHODHARY"},
    {"LAKSHMI", "LAXMI", "LAKSMI", "LUXMI"},
    {"MAHENDRA", "MAHENDER", "MAHINDER", "MAHINDRA", "MOHINDER"},
    {"SURENDRA", "SURENDER", "SURINDER", "SURINDRA"},
    {"RAJENDRA", "RAJENDER", "RAJINDER", "RAJINDRA"},
    {"NARENDRA", "NARENDER", "NARINDER"},
    {"VIRENDRA", "VIRENDER", "VIRINDER", "BIRENDER", "BIRENDRA"},
    {"DHARMENDRA", "DHARMENDER", "DHARMINDER"},
    {"YOGENDRA", "YOGENDER", "YOGINDER"},
    {"JITENDRA", "JITENDER", "JITENDAR", "JEETENDRA", "JEETENDER", "JITINDER"},
    {"SATYENDRA", "SATENDER", "SATYENDER", "SATINDER"},
    {"ESHWAR", "ISHWAR", "ISHVAR", "ESHVAR"},
    {"SHIV", "SHIVA", "SIV"},
    {"VIJAY", "BIJAY", "VIJAI"},
    {"VINOD", "BINOD"},
    {"VIKAS", "BIKAS"},
    {"YASH", "JASH"},
]

# Spellings that vary inside a word (eCourts must be asked for each). One-way
# pairs on purpose: "I" -> "EE" is a real variant (Jitender/Jeetender), but
# "S" -> "SH" or "A" -> "AA" would mostly invent spellings nobody uses.
# Most common variation first: only the first few survive the stem cap
MIDDLE_SWAPS = [
    ("EE", "I"), ("I", "EE"), ("SH", "S"), ("KSH", "X"), ("X", "KSH"), ("V", "W"), ("W", "V"),
    ("OO", "U"), ("PH", "F"), ("F", "PH"), ("Z", "J"), ("AA", "A"), ("CHH", "CH"), ("OU", "AU"), ("AU", "OU"),
    ("Q", "K"), ("U", "OO"),
]

_DIGRAPHS = [("KSH", "X"), ("CHH", "C"), ("CH", "C"), ("SH", "S"), ("DH", "D"), ("TH", "T"), ("BH", "B"),
             ("KH", "K"), ("GH", "G"), ("PH", "F"), ("JH", "J"), ("RH", "R"), ("Q", "K"), ("Z", "J"), ("W", "V")]


def clean(text: Optional[str]) -> str:
    """Uppercase letters and single spaces only."""
    text = (text or "").upper().replace("&", " AND ")
    text = re.sub(r"[^A-Z ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: Optional[str]) -> list[str]:
    return [t for t in clean(text).split() if t not in NOISE and len(t) > 1]


def skeleton(word: str) -> str:
    """
    Phonetic key that ignores the usual spelling variation in Indian names:
    first letter kept, aspirate digraphs merged, vowels (and w/y) dropped,
    doubled letters collapsed. JITENDER, JITENDRA, JEETENDAR -> JTNDR.
    """
    w = clean(word).replace(" ", "")
    if not w:
        return ""
    for family in FAMILIES:
        if w in family:
            w = sorted(family)[0]
            break
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    first, rest = w[0], w[1:]
    rest = re.sub(r"[AEIOUY]", "", rest)
    if first in "AEIOU":
        first = "A"  # Ishwar / Eshwar
    out = first + rest
    return re.sub(r"(.)\1+", r"\1", out)


def name_key(text: Optional[str]) -> str:
    """Space-separated skeletons of the meaningful words; stored in the case index."""
    return " ".join(skeleton(t) for t in tokens(text))


# --- What to send to eCourts ---

def _variants(word: str) -> list[str]:
    """Common spellings of one word, the typed one first; one change at a time."""
    for family in FAMILIES:
        if word in family:
            return [word] + sorted(family - {word})
    out = [word]
    for a, b in MIDDLE_SWAPS:
        # Only inside the word (first letters rarely vary), and only the
        # first occurrence: one-change variants stay realistic
        i = word.find(a, 1)
        if i > 0:
            v = word[:i] + b + word[i + len(a):]
            if v not in out:
                out.append(v)
    return out


def _stem(word: str) -> str:
    """Drop the endings that vary (-ER/-AR/-RA, a trailing vowel...) but keep 4+ letters."""
    if len(word) <= 4:
        return word
    keep = max(4, len(word) - 2)
    stem = word[:keep]
    stem = re.sub(r"[AEIOU]+$", "", stem)
    return stem if len(stem) >= 4 else word[:4]


def _cover(variants: list[str]) -> list[str]:
    """Literal stems covering the variants, the typed spelling's stem first."""
    chosen: list[str] = []
    for v in variants:
        s = _stem(v)
        if any(c in s for c in chosen):
            continue  # an existing stem already catches this spelling
        wider = [i for i, c in enumerate(chosen) if s in c]
        if wider:
            chosen[wider[0]] = s  # the shorter stem catches both
        else:
            chosen.append(s)
    return list(dict.fromkeys(chosen))


def pick_search_word(name: str) -> Optional[str]:
    """The most distinctive word of the name: not a title, not a very common name part, longest."""
    words = tokens(name)
    if not words:
        return None
    good = [w for w in words if w not in COMMON and len(w) >= 3]
    pool = good or [w for w in words if len(w) >= 3] or words
    return max(pool, key=len)


def search_stems(name: str, max_stems: int = 3) -> list[str]:
    """
    Literal strings to search eCourts with for `name`. Each is a substring of
    the common spellings of its most distinctive word, so a handful of
    searches covers Jitender / Jitendar / Jitendra / Jeetender.
    """
    words = tokens(name)
    if words and all(w in COMMON or len(w) < 3 for w in words):
        # "Ram Kumar": every word is common, so a stem of one would match
        # thousands of cases. Search the whole name as typed instead.
        return [" ".join(words)]
    word = pick_search_word(name)
    if not word:
        return []
    stems = [s for s in _cover(_variants(word)) if len(s) >= 3]  # eCourts minimum
    return stems[:max_stems] or [word]


# --- Ranking what eCourts returns ---

def _word_score(q: str, candidates: list[str]) -> float:
    if not candidates:
        return 0.0
    qs = skeleton(q)
    best = 0.0
    for c in candidates:
        if c == q:
            return 1.0
        cs = skeleton(c)
        if qs and qs == cs:
            best = max(best, 0.95)
            continue
        ratio = max(SequenceMatcher(None, q, c).ratio(), SequenceMatcher(None, qs, cs).ratio() * 0.95)
        if c.startswith(q) or q.startswith(c):
            ratio = max(ratio, 0.85 if min(len(q), len(c)) >= 4 else 0.6)
        best = max(best, ratio)
    return best


def name_score(query: str, text: Optional[str]) -> float:
    """0..1: how well `text` (a party field from eCourts) matches the typed name."""
    q_words = tokens(query)
    t_words = tokens(text)
    if not q_words or not t_words:
        return 0.0
    scores = [_word_score(q, t_words) for q in q_words]
    # Distinctive words count double; "Kumar" matching proves little
    weights = [1.0 if q in COMMON else 2.0 for q in q_words]
    return sum(s * w for s, w in zip(scores, weights)) / sum(weights)


def match_label(score: float) -> str:
    if score >= 0.99:
        return "Exact name"
    if score >= 0.88:
        return "Similar spelling"
    if score >= 0.7:
        return "Likely match"
    return "Possible match"
