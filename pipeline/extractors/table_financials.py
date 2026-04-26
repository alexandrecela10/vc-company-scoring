"""Table-aware extraction for financial/KPI grids in pitch decks.

This handles the pattern that flat regex cannot: one metric row crossed with
multiple period columns, e.g. ARR | 2024 | 2025 | 2026E | 2027P.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from pipeline.extractors.base import Observation
from pipeline.extractors.temporal import derive_scenario_and_date
from pipeline.preprocessors.base import Chunk

_PERIOD_RE = re.compile(
    r"^(?:FY\s*)?(?:20\d{2}|19\d{2})(?:[EPF])?$|^Q[1-4]\s*(?:20)?\d{2}(?:[EPF])?$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^-?\(?\$?\s*([\d,.]+)\s*%?\)?$")

_METRIC_ALIASES = {
    "arr": ("arr", "annual recurring revenue"),
    "revenue": ("revenue", "sales"),
    "gross_margin": ("gross margin", "gross margins", "gm"),
    "burn_rate": ("burn", "burn rate", "cash burn", "net burn"),
    "customer_count": ("customers", "paying customers", "customer count"),
    "employee_count": ("headcount", "employees", "fte", "team"),
}

_DEFAULT_UNITS = {
    "arr": "usd_millions",
    "revenue": "usd_millions",
    "burn_rate": "usd_millions",
    "gross_margin": "percent",
    "customer_count": "count",
    "employee_count": "count",
}

_GRANULARITY = {
    "arr": "year",
    "revenue": "year",
    "gross_margin": "year",
    "burn_rate": "month",
    "customer_count": "point_in_time",
    "employee_count": "point_in_time",
}


def extract_table_observations(chunks: Iterable[Chunk], rules: Dict[str, Dict[str, Any]]) -> List[Observation]:
    """Extract observations from table metadata attached by PitchDeckPreprocessor."""
    observations: List[Observation] = []
    for chunk in chunks:
        for table_idx, table in enumerate(chunk.metadata.get("tables") or []):
            observations.extend(_extract_from_table(chunk, table, table_idx, rules))
    return observations


def _extract_from_table(
    chunk: Chunk, table: List[List[Optional[str]]], table_idx: int, rules: Dict[str, Dict[str, Any]]
) -> List[Observation]:
    rows = [_clean_row(r) for r in table]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return []

    header_idx, period_cols = _find_header(rows)
    if header_idx is None or not period_cols:
        return []

    units = _infer_table_units(rows)
    out: List[Observation] = []
    for row_idx, row in enumerate(rows[header_idx + 1 :], start=header_idx + 1):
        metric = _metric_code_for_row(row)
        if metric is None or metric not in rules:
            continue
        cfg = rules[metric]
        for col_idx, period_label in period_cols.items():
            if col_idx >= len(row):
                continue
            value = _parse_cell_value(row[col_idx], metric, units)
            if value is None or not _within_range(value, cfg.get("validate")):
                continue
            scenario, as_of_date = derive_scenario_and_date(
                period_label, (cfg.get("temporal") or {}).get("period_granularity")
            )
            if scenario is None or as_of_date is None:
                continue
            evidence = _evidence(row, period_label, row[col_idx])
            out.append(
                Observation(
                    metric_name=metric,
                    value=_value_to_string(value),
                    chunk_locator=chunk.locator,
                    evidence_text=evidence,
                    method="table",
                    confidence=0.95,
                    method_details={
                        "table_index": table_idx,
                        "row_index": row_idx,
                        "column_index": col_idx,
                        "period_label": period_label,
                        "unit_hint": units,
                    },
                    as_of_date=as_of_date,
                    period_granularity=(cfg.get("temporal") or {}).get("period_granularity") or _GRANULARITY.get(metric),
                    scenario=scenario,
                    currency="USD" if cfg.get("unit") == "usd" else None,
                    period_label=period_label,
                )
            )
    return out


def _clean_row(row: List[Optional[str]]) -> List[str]:
    return [" ".join(str(c or "").replace("\n", " ").split()).strip() for c in row]


def _find_header(rows: List[List[str]]) -> tuple[Optional[int], Dict[int, str]]:
    for i, row in enumerate(rows[:5]):
        cols = {j: c for j, c in enumerate(row) if _PERIOD_RE.match(c.replace("'", ""))}
        if len(cols) >= 2:
            return i, cols
    return None, {}


def _infer_table_units(rows: List[List[str]]) -> str:
    text = " ".join(" ".join(r) for r in rows[:3]).lower()
    if "%" in text or "margin" in text:
        return "mixed"
    if "$" in text and ("m" in text or "million" in text):
        return "usd_millions"
    if "$" in text and ("k" in text or "thousand" in text):
        return "usd_thousands"
    if "$" in text:
        return "usd"
    if "000" in text or "thousand" in text:
        return "thousands"
    if "m)" in text or "($m" in text or "usd m" in text or "millions" in text:
        return "usd_millions"
    return "mixed"


def _metric_code_for_row(row: List[str]) -> Optional[str]:
    label = next((c for c in row if c), "").lower()
    label = re.sub(r"[^a-z0-9% ]+", " ", label)
    label = " ".join(label.split())
    for code, aliases in _METRIC_ALIASES.items():
        if any(alias in label for alias in aliases):
            return code
    return None


def _parse_cell_value(cell: str, metric: str, table_units: str) -> Optional[float]:
    raw = (cell or "").strip()
    if not raw or raw in {"-", "—", "n/a", "N/A"}:
        return None
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-")
    m = _NUMBER_RE.match(raw.replace(",", ""))
    if not m:
        return None
    value = float(m.group(1))
    if negative:
        value = -value
    if metric == "gross_margin" or "%" in raw:
        return value
    if metric in {"customer_count", "employee_count"}:
        return abs(value)
    units = table_units if table_units != "mixed" else _DEFAULT_UNITS.get(metric, "count")
    if units == "usd_millions":
        return abs(value) * 1_000_000
    if units == "usd_thousands" or units == "thousands":
        return abs(value) * 1_000
    return abs(value)


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


def _evidence(row: List[str], period_label: str, cell: str) -> str:
    label = next((c for c in row if c), "metric")
    return f"{label} | {period_label} | {cell}"
