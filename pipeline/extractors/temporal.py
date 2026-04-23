"""Shared period-label parsing for both deterministic and LLM extractors.

Why a dedicated module:
  Both extraction paths need to turn labels like "2024", "2026E", "Q4 2024",
  or "Dec 2024" into a (scenario, as_of_date) pair. Keeping the logic here
  (instead of inside llm_fallback.py) lets the deterministic engine capture
  inline phrases like "$2.4M ARR in 2024" without calling the LLM.

Suffix convention:
    suffix 'E'  -> estimate      (e.g. "2026E")
    suffix 'P'  -> projection    (e.g. "2028P")
    suffix 'F'  -> forecast      (rare; synonymous with projection)
    no suffix   -> actual

as_of_date is always the END of the period so a single DATE comparison works
for "latest actual" ordering.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Label regexes (compiled once; used by both the explicit parser and the
# sliding-window scanner).
# ---------------------------------------------------------------------------

# "2024", "2026E", "FY2024"  -- a bare 4-digit year with optional scenario suffix.
_YEAR_LABEL_RE = re.compile(
    r"^(?:FY[\s-]?)?(?P<year>(?:19|20)\d{2})(?P<suffix>[EPF])?$",
    re.IGNORECASE,
)
# "Q4 2024", "Q2'24" -- quarter + year.
_QUARTER_LABEL_RE = re.compile(
    r"^Q(?P<q>[1-4])[\s']*(?P<year>(?:19|20)?\d{2})(?P<suffix>[EPF])?$",
    re.IGNORECASE,
)
# "Dec 2024", "December 2024".
_MONTH_LABEL_RE = re.compile(
    r"^(?P<month>[A-Za-z]{3,9})[\s-]+(?P<year>(?:19|20)\d{2})(?P<suffix>[EPF])?$",
)

# Same three as ANY-MATCH regexes for the sliding-window scanner. Order
# matters: month-word and quarter are more specific than bare year, so we
# try them first to avoid "Dec 2024" being parsed as "2024" only.
_SCAN_MONTH_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[\s-]+(?P<year>(?:19|20)\d{2})(?P<suffix>[EPF])?\b",
    re.IGNORECASE,
)
_SCAN_QUARTER_RE = re.compile(
    r"\bQ(?P<q>[1-4])[\s']*(?P<year>(?:19|20)\d{2})(?P<suffix>[EPF])?\b",
    re.IGNORECASE,
)
_SCAN_YEAR_RE = re.compile(
    r"\b(?:FY[\s-]?)?(?P<year>(?:19|20)\d{2})(?P<suffix>[EPF])?\b",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Quarter-end month/day tuples. No leap-year logic needed -- Q1 ends Mar 31,
# Q2 ends Jun 30, Q3 ends Sep 30, Q4 ends Dec 31.
_QUARTER_END_MD = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _suffix_to_scenario(suffix: Optional[str]) -> str:
    """Map a bare suffix letter to a scenario enum. Default is `actual`."""
    if not suffix:
        return "actual"
    return {"E": "estimate", "P": "projection", "F": "forecast"}.get(
        suffix.upper(), "actual"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def derive_scenario_and_date(
    period_label: Optional[str], granularity: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Parse a period_label into (scenario, as_of_date ISO string).

    Returns (None, None) if the label is empty or unparseable. Callers then
    decide whether to reject the observation (when rules.yaml declares
    `temporal.requires`) or accept it with NULL temporal fields.

    `granularity` is currently informational -- the date is always the
    PERIOD-END, regardless. Kept in the signature so future callers can
    pass a hint (e.g. "month" -> treat "2024" as Dec 2024 anyway, same
    result) without a breaking change.
    """
    if not period_label:
        return None, None
    label = period_label.strip()

    m = _YEAR_LABEL_RE.match(label)
    if m:
        year = int(m.group("year"))
        return _suffix_to_scenario(m.group("suffix")), f"{year:04d}-12-31"

    m = _QUARTER_LABEL_RE.match(label)
    if m:
        q = int(m.group("q"))
        year_raw = m.group("year")
        year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
        mm, dd = _QUARTER_END_MD[q]
        return _suffix_to_scenario(m.group("suffix")), f"{year:04d}-{mm:02d}-{dd:02d}"

    m = _MONTH_LABEL_RE.match(label)
    if m:
        mn = _MONTH_NAMES.get(m.group("month").lower())
        if mn is not None:
            year = int(m.group("year"))
            dd = 28 if mn == 2 else (30 if mn in (4, 6, 9, 11) else 31)
            return _suffix_to_scenario(m.group("suffix")), f"{year:04d}-{mn:02d}-{dd:02d}"

    return None, None


def scan_nearby_period_label(
    text: str, start: int, end: int, window: int = 60
) -> Optional[str]:
    """Look for a period label (year / quarter / month+year) near a match.

    The deterministic engine calls this after a regex value match to enrich
    the observation with temporal info. Example: a chunk containing
    "ARR: $2.4M in 2024" matches the ARR regex at "$2.4M", then this scan
    finds "2024" in the nearby window and returns "2024".

    `window` is the number of characters on EACH side of (start, end) we
    consider. Keep it tight so a stray year from an unrelated sentence
    doesn't contaminate the match.
    """
    if not text or start >= end:
        return None
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    neighbourhood = text[lo:hi]

    # Try the most specific patterns first. Return the FIRST hit (any of the
    # three regexes is unambiguous; they don't overlap in what they match).
    for pat in (_SCAN_MONTH_RE, _SCAN_QUARTER_RE, _SCAN_YEAR_RE):
        m = pat.search(neighbourhood)
        if m:
            # Return the verbatim label so downstream can persist it for audit.
            return m.group(0)
    return None
