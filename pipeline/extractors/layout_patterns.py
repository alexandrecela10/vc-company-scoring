"""Reusable deterministic extractors for common pitch-deck layout patterns."""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from pipeline.extractors.base import Observation
from pipeline.extractors.temporal import derive_scenario_and_date
from pipeline.preprocessors.base import Chunk

_MONEY_RE = re.compile(r"\$\s*(?P<amount>[\d.]+)\s*(?P<unit>k|m|b|million|billion)?", re.IGNORECASE)
_DECK_DATE_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<year>20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def extract_layout_observations(chunks: Iterable[Chunk], rules: Dict[str, Dict[str, Any]]) -> List[Observation]:
    """Run deck-pattern extractors that need broader layout context than regex."""
    chunk_list = list(chunks)
    deck_date = infer_deck_date(chunk_list)
    observations: List[Observation] = []
    for chunk in chunk_list:
        observations.extend(_extract_flat_financial_matrix(chunk, rules))
        observations.extend(_extract_kpi_cards(chunk, rules, deck_date))
        observations.extend(_extract_milestones(chunk, rules))
        observations.extend(_extract_runway(chunk, rules, deck_date))
    return _dedupe_observations(observations)


def _dedupe_observations(observations: List[Observation]) -> List[Observation]:
    """Keep first equivalent layout fact; drop near-identical duplicates.

    Equivalence key intentionally ignores period_label because the same period
    can appear as both "2028" and "2028P" with identical scenario/as_of_date.
    """
    out: List[Observation] = []
    seen = set()
    for obs in observations:
        key = (
            obs.metric_name,
            obs.value,
            obs.chunk_locator,
            obs.as_of_date,
            obs.scenario,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(obs)
    return out


def _matrix_period_labels(text: str) -> List[str]:
    # Prefer the explicit header row directly before ARR/Revenue labels so
    # incidental years in narrative text (e.g. "Path to $52M ARR by 2028")
    # do not shift the whole column alignment.
    anchor = re.search(r"\bARR\s*\(\$M\)|\bRevenue\s*\(\$M\)", text, re.IGNORECASE)
    if anchor:
        prefix = text[max(0, anchor.start() - 120):anchor.start()]
        header_periods = re.findall(r"\b20\d{2}[EPF]?\b", prefix)
        if len(header_periods) >= 3:
            return header_periods[-5:] if len(header_periods) > 5 else header_periods
    periods = re.findall(r"\b20\d{2}[EPF]?\b", text)
    return periods[-5:] if len(periods) > 5 else periods


def infer_deck_date(chunks: Iterable[Chunk]) -> Optional[str]:
    """Return the last day of the first month/year visible near the deck front."""
    for chunk in list(chunks)[:3]:
        match = _DECK_DATE_RE.search(chunk.text)
        if not match:
            continue
        month = _MONTHS.get(match.group("month").lower())
        year = int(match.group("year"))
        if month is None:
            continue
        return f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"
    return None


def _extract_kpi_cards(chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str]) -> List[Observation]:
    text = chunk.text
    if re.search(r"\b(?:reach|hit|get\s+to|grow\s+team)\b", text, re.IGNORECASE):
        return []
    if not re.search(r"\b(?:traction|kpi|metrics?)\b", text, re.IGNORECASE):
        return []
    observations: List[Observation] = []
    observations.extend(_extract_money_label_card(chunk, rules, deck_date, "arr", r"\bARR\b"))
    observations.extend(_extract_ordered_kpi_pair(chunk, rules, deck_date))
    if not any(o.metric_name == "customer_count" for o in observations):
        observations.extend(_extract_count_label_card(chunk, rules, deck_date, "customer_count", r"paying\s+customers|customers"))
    observations.extend(_extract_percent_label_card(chunk, rules, deck_date, "gross_margin", r"gross\s+margin|gm"))
    if "Net revenue retention" in text or "NRR" in text:
        return observations
    return observations


def _extract_flat_financial_matrix(chunk: Chunk, rules: Dict[str, Dict[str, Any]]) -> List[Observation]:
    periods = _matrix_period_labels(chunk.text)
    if len(periods) < 3 or not re.search(r"\bfinancials?\b|\bARR\b.*\bRevenue\b", chunk.text, re.IGNORECASE):
        return []
    periods = periods[:5]
    specs = [
        ("arr", r"ARR\s*\(\$M\)\s+(?P<values>[\d.\s]+)"),
        ("revenue", r"Revenue\s*\(\$M\)\s+(?P<values>[\d.\s]+)"),
        ("gross_margin", r"Gross\s+margin\s+(?P<values>(?:-?\d{1,3}%\s*)+)"),
        ("burn_rate", r"Burn\s*\(\$M\)\s+(?P<values>(?:\(?-?[\d.]+\)?\s*)+)"),
        ("customer_count", r"Customers\s+(?P<values>(?:\d+\s*)+)"),
        ("employee_count", r"Headcount\s+(?P<values>(?:\d+\s*)+)"),
    ]
    observations: List[Observation] = []
    for metric, pattern in specs:
        if metric not in rules:
            continue
        match = re.search(pattern, chunk.text, re.IGNORECASE)
        if not match:
            continue
        raw_values = re.findall(r"\(?-?\d+(?:\.\d+)?%?\)?", match.group("values"))
        for period_label, raw_value in zip(periods, raw_values):
            value = _matrix_value(metric, raw_value)
            if value is None or not _within_range(value, rules[metric].get("validate")):
                continue
            scenario, as_of_date = derive_scenario_and_date(period_label)
            if scenario is None or as_of_date is None:
                continue
            observations.append(
                _observation(
                    chunk,
                    rules[metric],
                    metric,
                    value,
                    f"{metric} | {period_label} | {raw_value}",
                    as_of_date,
                    scenario,
                    period_label,
                )
            )
    return observations


def _extract_money_label_card(
    chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str], metric: str, label_pattern: str
) -> List[Observation]:
    if metric not in rules or deck_date is None:
        return []
    text = chunk.text
    label = re.search(label_pattern, text, re.IGNORECASE)
    if not label:
        return []
    candidates = [m for m in _MONEY_RE.finditer(text) if abs(m.start() - label.start()) <= 90]
    if not candidates:
        return []
    match = min(candidates, key=lambda m: abs(m.start() - label.start()))
    value = _money_to_number(match.group("amount"), match.group("unit"))
    if value is None or not _within_range(value, rules[metric].get("validate")):
        return []
    evidence = _context(text, match.start(), label.end())
    return [_observation(chunk, rules[metric], metric, value, evidence, deck_date, "actual", "deck_date")]


def _extract_ordered_kpi_pair(chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str]) -> List[Observation]:
    if "customer_count" not in rules or deck_date is None:
        return []
    match = re.search(
        r"\$\s*[\d.]+\s*(?:k|m|b|million|billion)?\s+(?P<customers>\d{1,6})\s+"
        r"\d{1,3}(?:\.\d+)?%\s+\$\s*[\d.]+\s*(?:k|m|b|million|billion)?\s+"
        r"ARR\s+Paying\s+customers\b",
        chunk.text,
        re.IGNORECASE,
    )
    if not match:
        return []
    value = float(match.group("customers"))
    if not _within_range(value, rules["customer_count"].get("validate")):
        return []
    return [
        _observation(
            chunk,
            rules["customer_count"],
            "customer_count",
            value,
            match.group(0),
            deck_date,
            "actual",
            "deck_date",
        )
    ]


def _extract_count_label_card(
    chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str], metric: str, label_pattern: str
) -> List[Observation]:
    if metric not in rules or deck_date is None:
        return []
    text = chunk.text
    label = re.search(label_pattern, text, re.IGNORECASE)
    if not label:
        return []
    before = text[max(0, label.start() - 50):label.start()]
    nums = list(re.finditer(r"\b(?P<amount>\d{1,6})\b", before))
    if not nums:
        return []
    match = nums[-1]
    value = float(match.group("amount"))
    if not _within_range(value, rules[metric].get("validate")):
        return []
    start = max(0, label.start() - 50) + match.start()
    evidence = _context(text, start, label.end())
    return [_observation(chunk, rules[metric], metric, value, evidence, deck_date, "actual", "deck_date")]


def _extract_percent_label_card(
    chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str], metric: str, label_pattern: str
) -> List[Observation]:
    if metric not in rules or deck_date is None or not re.search(label_pattern, chunk.text, re.IGNORECASE):
        return []
    match = re.search(r"\b(?P<amount>-?\d{1,3}(?:\.\d+)?)\s*%\s+(?:gross\s+margin|gm)\b", chunk.text, re.IGNORECASE)
    if not match:
        return []
    value = float(match.group("amount"))
    if not _within_range(value, rules[metric].get("validate")):
        return []
    evidence = _context(chunk.text, match.start(), match.end())
    return [_observation(chunk, rules[metric], metric, value, evidence, deck_date, "actual", "deck_date")]


def _extract_milestones(chunk: Chunk, rules: Dict[str, Dict[str, Any]]) -> List[Observation]:
    out: List[Observation] = []
    if "arr" in rules:
        for match in re.finditer(r"\b(?:reach|hit|get\s+to|path\s+to)\s+\$\s*(?P<amount>[\d.]+)\s*(?P<unit>k|m|b|million|billion)?\s+ARR\s+by\s+(?:end\s+of\s+)?(?P<year>20\d{2})\b", chunk.text, re.IGNORECASE):
            value = _money_to_number(match.group("amount"), match.group("unit"))
            if value is None or not _within_range(value, rules["arr"].get("validate")):
                continue
            scenario, as_of_date = derive_scenario_and_date(match.group("year"))
            out.append(_observation(chunk, rules["arr"], "arr", value, match.group(0), as_of_date, "projection", match.group("year")))
    if "employee_count" in rules:
        for match in re.finditer(r"\bgrow\s+team\s+from\s+\d{1,6}\s+to\s+(?P<amount>\d{1,6})\b", chunk.text, re.IGNORECASE):
            year = _nearest_future_year(chunk.text, match.start(), match.end())
            if year is None:
                continue
            value = float(match.group("amount"))
            if not _within_range(value, rules["employee_count"].get("validate")):
                continue
            out.append(_observation(chunk, rules["employee_count"], "employee_count", value, match.group(0), f"{year}-12-31", "projection", year))
    return out


def _extract_runway(chunk: Chunk, rules: Dict[str, Dict[str, Any]], deck_date: Optional[str]) -> List[Observation]:
    if "runway_months" not in rules or deck_date is None:
        return []
    match = re.search(r"\b(?P<amount>\d{1,3})[-\s]?months?\s+runway\b|\brunway[:\s]+(?P<amount2>\d{1,3})\s+months?\b", chunk.text, re.IGNORECASE)
    if not match:
        return []
    amount = match.group("amount") or match.group("amount2")
    value = float(amount)
    if not _within_range(value, rules["runway_months"].get("validate")):
        return []
    return [_observation(chunk, rules["runway_months"], "runway_months", value, match.group(0), deck_date, "actual", "deck_date")]


def _observation(
    chunk: Chunk,
    cfg: Dict[str, Any],
    metric: str,
    value: float,
    evidence: str,
    as_of_date: Optional[str],
    scenario: Optional[str],
    period_label: str,
) -> Observation:
    return Observation(
        metric_name=metric,
        value=_value_to_string(value),
        chunk_locator=chunk.locator,
        evidence_text=evidence,
        method="layout_pattern",
        confidence=0.86,
        method_details={"pattern_type": period_label},
        as_of_date=as_of_date,
        period_granularity=(cfg.get("temporal") or {}).get("period_granularity"),
        scenario=scenario,
        currency="USD" if cfg.get("unit") == "usd" else None,
        period_label=period_label,
    )


def _money_to_number(amount: str, unit: Optional[str]) -> Optional[float]:
    try:
        value = float(amount.replace(",", ""))
    except ValueError:
        return None
    unit_key = (unit or "").lower()
    if unit_key in {"k"}:
        return value * 1_000
    if unit_key in {"m", "million"}:
        return value * 1_000_000
    if unit_key in {"b", "billion"}:
        return value * 1_000_000_000
    return value


def _matrix_value(metric: str, raw: str) -> Optional[float]:
    cleaned = raw.strip().replace("%", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")") or cleaned.startswith("-")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if metric in {"arr", "revenue", "burn_rate"}:
        value = abs(value) * 1_000_000
    elif metric in {"customer_count", "employee_count"}:
        value = abs(value)
    elif negative:
        value = -value
    return value


def _nearest_future_year(text: str, start: int, end: int) -> Optional[str]:
    window = text[max(0, start - 120):min(len(text), end + 120)]
    match = re.search(r"\b(20\d{2})\b", window)
    return match.group(1) if match else None


def _within_range(value: float, validate: Optional[Dict[str, Any]]) -> bool:
    if not validate:
        return True
    if "min" in validate and value < float(validate["min"]):
        return False
    if "max" in validate and value > float(validate["max"]):
        return False
    return True


def _value_to_string(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _context(text: str, start: int, end: int, window: int = 40) -> str:
    return text[max(0, start - window):min(len(text), end + window)]
