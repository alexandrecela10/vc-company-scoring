"""
Score Engine — Option B multiplicative formula.

HOW THE FORMULA WORKS (Option B):

  Step 1 — Per-metric score
    Each metric has a numeric value (stored as text, cast here) and a weight.

  Step 2 — Metric type score
    weighted_avg of all child metric scores within the type.

  Step 3 — Overall score
    weighted_avg(all type scores) × PRODUCT(must_have_type_score / 5.0)

  The multiplicative penalty means:
    - A must-have type scoring 1/5 caps the overall at ~20% of its potential.
    - A must-have type with NO value at all → overall = None (incomplete).

  This matches VC intuition: one fatal flaw kills a deal regardless of other strengths.

WHY PURE PYTHON (not just the SQL view)?
  The SQL view in schema.sql handles the DB-side computation efficiently.
  This Python layer adds:
  - Structured data objects (dicts) the UI can render directly
  - Per-metric-type breakdowns with evidence attached
  - Gap detection (which must-haves are missing and why)
  - Weight override (analyst personal weights vs house)
"""

import math
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes returned by the scorer
# ---------------------------------------------------------------------------

@dataclass
class MetricScore:
    """Score for one individual metric for one company."""
    metric_id: str
    metric_name: str
    metric_code: Optional[str]
    value_raw: Optional[str]         # Raw string from DB ("true", "4", "185000")
    value_numeric: Optional[float]   # Cast to float for formula (None if not computable)
    weight: float
    must_have: bool
    obtain_method: str
    source_name: Optional[str]       # Which data source provided this value
    raw_evidence: Optional[str]      # The proof quote (text only)
    evidence_url: Optional[str]      # Source link (separate column, clickable in UI)
    url_verified: bool               # True iff quote was confirmed on the page
    confidence: float
    override: bool                   # True = analyst-locked, agents cannot overwrite
    override_reason: Optional[str]
    captured_by: str
    captured_at: str
    period_granularity: Optional[str] = None  # "month" | "year" | ...; None when the source row lacks it


@dataclass
class MetricTypeScore:
    """Weighted-average score for one metric type (e.g. 'Founders Strength')."""
    metric_type_id: str
    metric_type_name: str
    must_have: bool
    house_weight: float              # Institutional weight used in overall formula
    type_score: Optional[float]      # None if no scoreable metrics exist for this type
    metric_scores: List[MetricScore] = field(default_factory=list)
    missing_must_haves: List[str] = field(default_factory=list)  # Names of missing must-haves
    grounded_formula: Optional[str] = None
    grounded_formula_inputs: List[Dict] = field(default_factory=list)


@dataclass
class CompanyScorecard:
    """Full scorecard for one company."""
    company_id: str
    company_name: str
    pipeline_stage: str
    overall_score: Optional[float]   # None if any must-have type has no value
    is_complete: bool                # False if any must-have type is missing
    missing_must_have_types: List[str] = field(default_factory=list)
    must_have_multiplier: float = 1.0
    weighted_avg_base: Optional[float] = None
    type_scores: List[MetricTypeScore] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Value casting
# ---------------------------------------------------------------------------

def _cast_to_numeric(value_raw: Optional[str], value_type: str = "number") -> Optional[float]:
    """
    Cast a raw DB string value to a float for formula use.

    Rules:
    - "true"  → 5.0  (boolean yes maps to max score on 1–5 scale)
    - "false" → 1.0  (boolean no maps to min score)
    - numeric string → float directly
    - anything else → None (not scoreable)

    Why true→5 and false→1 instead of 1/0?
    Because all metrics live on the same 1–5 scale in the formula.
    A boolean "Has Patent = true" contributes 5.0, same as a "score_1_5 = 5".
    """
    if value_raw is None:
        return None
    v = value_raw.strip().lower()
    if v == "true":
        return 5.0
    if v == "false":
        return 1.0
    try:
        num = float(v)
        # Clamp number metrics to 1–5 range if they look like scores
        if value_type == "score_1_5":
            return max(1.0, min(5.0, num))
        # For raw numbers (MRR, headcount) we do NOT score them directly —
        # they appear in the evidence panel but don't feed the formula.
        # Return None so the formula skips them.
        if value_type == "number":
            return None
        return num
    except (ValueError, TypeError):
        return None


def _to_float(value_raw: Optional[str]) -> Optional[float]:
    """Best-effort numeric parse for raw values used in custom formulas."""
    if value_raw is None:
        return None
    try:
        return float(str(value_raw).strip())
    except (ValueError, TypeError):
        return None


def _clamp_score_1_to_5(value: float) -> float:
    """Clamp any numeric sub-score into the canonical 1-5 scoring band."""
    return max(1.0, min(5.0, value))


def _grounded_financials_formula(metric_scores: List[MetricScore]) -> Tuple[Optional[float], Optional[str], List[Dict]]:
    """Compute a grounded Financials score from concrete metric claims.

    Formula (when inputs are available):
      gross_margin_score = clamp(gross_margin_pct / 20, 1, 5)
      runway_score       = clamp(runway_months / 6, 1, 5)
      burn_eff_score     = clamp(1 + 2*(revenue / annual_burn), 1, 5)
                           annual_burn = burn_rate x 12 when the burn is monthly
      stage_score        = funding_stage (already 1-5)

      financials_score = weighted_avg(available components)
      weights: gross_margin=0.35, runway=0.25, burn_eff=0.25, funding_stage=0.15

    Every component is grounded in an existing metric claim from company_metric_value.
    """
    by_code = {ms.metric_code: ms for ms in metric_scores if ms.metric_code}
    inputs: List[Dict] = []
    components: List[Tuple[str, float, float]] = []

    def add_component(name: str, score: float, weight: float, ms: MetricScore, raw_value: Optional[float]) -> None:
        components.append((name, score, weight))
        inputs.append(
            {
                "component": name,
                "metric": ms.metric_name,
                "metric_code": ms.metric_code,
                "raw_value": ms.value_raw,
                "derived_score": round(score, 2),
                "weight": weight,
                "source_name": ms.source_name,
                "evidence": ms.raw_evidence,
                "evidence_url": ms.evidence_url,
                "formula_detail": f"raw={raw_value}" if raw_value is not None else None,
            }
        )

    gross_margin = by_code.get("gross_margin")
    gm_raw = _to_float(gross_margin.value_raw) if gross_margin else None
    if gross_margin and gm_raw is not None:
        gm_score = _clamp_score_1_to_5(gm_raw / 20.0)
        add_component("gross_margin_score", gm_score, 0.35, gross_margin, gm_raw)

    runway = by_code.get("runway_months")
    runway_raw = _to_float(runway.value_raw) if runway else None
    if runway and runway_raw is not None:
        runway_score = _clamp_score_1_to_5(runway_raw / 6.0)
        add_component("runway_score", runway_score, 0.25, runway, runway_raw)

    revenue = by_code.get("revenue")
    burn = by_code.get("burn_rate")
    revenue_raw = _to_float(revenue.value_raw) if revenue else None
    burn_raw = _to_float(burn.value_raw) if burn else None
    if revenue and burn and revenue_raw is not None and burn_raw is not None and burn_raw > 0:
        # Revenue is annual; burn is often monthly. Compare like with like.
        annual_burn = burn_raw * 12 if burn.period_granularity == "month" else burn_raw
        burn_eff = _clamp_score_1_to_5(1.0 + 2.0 * (revenue_raw / annual_burn))
        add_component("burn_efficiency_score", burn_eff, 0.25, revenue, revenue_raw)

    stage = by_code.get("funding_stage")
    if stage and stage.value_numeric is not None:
        stage_score = _clamp_score_1_to_5(stage.value_numeric)
        add_component("funding_stage_score", stage_score, 0.15, stage, stage.value_numeric)

    if not components:
        return None, None, []

    total_weight = sum(w for _, _, w in components)
    final_score = sum(score * weight for _, score, weight in components) / total_weight
    final_score = round(final_score, 2)
    formula = (
        "FinancialsScore = weighted_avg("
        "gross_margin/20, runway_months/6, 1+2*(revenue/annual_burn), funding_stage"
        ") on available inputs, then clamped to 1-5 per component"
    )
    return final_score, formula, inputs


# ---------------------------------------------------------------------------
# Core scoring functions
# ---------------------------------------------------------------------------

def _score_metric_type(
    type_row: Dict,
    metric_value_rows: List[Dict],
) -> MetricTypeScore:
    """
    Compute the weighted-average score for one metric type.

    Args:
        type_row:           One row from metric_type table
        metric_value_rows:  All latest CompanyMetricValue rows for this type+company

    Returns a MetricTypeScore with all child MetricScores attached.
    """
    metric_scores: List[MetricScore] = []
    missing_must_haves: List[str] = []

    for row in metric_value_rows:
        m = row.get("metric", {}) or {}
        source = row.get("data_source", {}) or {}

        # Cast raw value to numeric for formula
        value_numeric = _cast_to_numeric(
            row.get("value"),
            m.get("value_type", "number"),
        )

        metric_scores.append(MetricScore(
            metric_id=row["metric_id"],
            metric_name=m.get("name", "Unknown"),
            metric_code=m.get("code"),
            value_raw=row.get("value"),
            value_numeric=value_numeric,
            weight=float(m.get("weight", 1.0)),
            must_have=bool(m.get("must_have", False)),
            obtain_method=m.get("obtain_method", "manual"),
            source_name=source.get("name"),
            raw_evidence=row.get("raw_evidence"),
            evidence_url=row.get("evidence_url"),
            url_verified=bool(row.get("url_verified", False)),
            confidence=float(row.get("confidence", 1.0)),
            override=bool(row.get("override", False)),
            override_reason=row.get("override_reason"),
            captured_by=row.get("captured_by", ""),
            captured_at=str(row.get("captured_at", "")),
            period_granularity=row.get("period_granularity"),
        ))

    # Identify must-have metrics with no scoreable value
    for ms in metric_scores:
        if ms.must_have and ms.value_numeric is None:
            missing_must_haves.append(ms.metric_name)

    # Weighted average: only include metrics with a numeric score
    scoreable = [ms for ms in metric_scores if ms.value_numeric is not None]
    if not scoreable:
        type_score = None
    else:
        total_weight = sum(ms.weight for ms in scoreable)
        if total_weight == 0:
            type_score = None
        else:
            # Weighted sum divided by total weight = weighted average
            type_score = sum(ms.value_numeric * ms.weight for ms in scoreable) / total_weight
            type_score = round(type_score, 2)

    grounded_formula = None
    grounded_formula_inputs: List[Dict] = []
    # First grounded formula rollout: Financials has the richest measured data
    # (ARR/Revenue/GM/Burn/Runway/Stage) and highest business relevance.
    if type_row.get("name") == "Financials":
        fin_score, fin_formula, fin_inputs = _grounded_financials_formula(metric_scores)
        if fin_score is not None:
            type_score = fin_score
            grounded_formula = fin_formula
            grounded_formula_inputs = fin_inputs

    return MetricTypeScore(
        metric_type_id=type_row["id"],
        metric_type_name=type_row["name"],
        must_have=bool(type_row.get("must_have", False)),
        house_weight=float(type_row.get("house_weight", 1.0)),
        type_score=type_score,
        metric_scores=metric_scores,
        missing_must_haves=missing_must_haves,
        grounded_formula=grounded_formula,
        grounded_formula_inputs=grounded_formula_inputs,
    )


def score_company(company_id: str, user_id: str = "house") -> CompanyScorecard:
    """
    Compute the full scorecard for one company.

    This is the main entry point called by the UI and agents.

    Args:
        company_id: UUID of the company to score
        user_id:    Whose weights to use. "house" = institutional defaults.

    Returns a CompanyScorecard with overall_score and full breakdown.

    Option B formula:
        base  = weighted_avg(type_scores, weights=user_weights)
        penalty = PRODUCT(must_have_score / 5.0)  for each must-have type
        overall = base × penalty

    If ANY must-have metric type has type_score = None → overall = None.
    """
    # --- Fetch data ---
    company = db.get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found.")

    all_metric_types = db.get_metric_types()
    latest_values = db.get_latest_values_for_company(company_id)
    weights = db.get_weights(user_id)

    # Group latest values by metric_type_id for fast lookup
    # Each row has metric.metric_type_id nested inside
    values_by_type: Dict[str, List[Dict]] = {}
    for row in latest_values:
        m = row.get("metric", {}) or {}
        tid = m.get("metric_type_id")
        if tid:
            values_by_type.setdefault(tid, []).append(row)

    # --- Score each metric type ---
    type_scores: List[MetricTypeScore] = []
    for type_row in all_metric_types:
        tid = type_row["id"]
        rows_for_type = values_by_type.get(tid, [])
        mts = _score_metric_type(type_row, rows_for_type)
        # Override house_weight with user-specific weight if available
        if tid in weights:
            mts.house_weight = weights[tid]
        type_scores.append(mts)

    # --- Check must-have completeness ---
    missing_must_have_types = [
        mts.metric_type_name
        for mts in type_scores
        if mts.must_have and mts.type_score is None
    ]
    is_complete = len(missing_must_have_types) == 0

    # --- Compute base weighted average (all types) ---
    scoreable_types = [mts for mts in type_scores if mts.type_score is not None]
    if not scoreable_types:
        weighted_avg_base = None
    else:
        total_w = sum(mts.house_weight for mts in scoreable_types)
        weighted_avg_base = (
            sum(mts.type_score * mts.house_weight for mts in scoreable_types) / total_w
            if total_w > 0 else None
        )

    # --- Compute must-have penalty (Option B, weakest-link) ---
    # The penalty is driven by the WEAKEST must-have score, not the product of all.
    # Why not product? With 7 must-haves averaging 4/5, product = 0.8^7 = 0.21
    # would collapse a 4/5-average company to ~1/5. Too punishing.
    # Weakest-link matches the VC intuition: "one fatal flaw kills the deal".
    # A must-have scoring 1/5 → multiplier 0.2 (overall capped at 20% of base)
    # A must-have scoring 5/5 → multiplier 1.0 (no penalty)
    must_have_scores = [
        mts.type_score for mts in type_scores
        if mts.must_have and mts.type_score is not None
    ]
    if must_have_scores:
        must_have_multiplier = min(must_have_scores) / 5.0
    else:
        must_have_multiplier = 1.0

    # --- Overall score ---
    if not is_complete or weighted_avg_base is None:
        # Missing must-have → score is not computable
        overall_score = None
    else:
        overall_score = round(weighted_avg_base * must_have_multiplier, 2)

    return CompanyScorecard(
        company_id=company_id,
        company_name=company["name"],
        pipeline_stage=company.get("pipeline_stage", "deal_sourcing"),
        overall_score=overall_score,
        is_complete=is_complete,
        missing_must_have_types=missing_must_have_types,
        must_have_multiplier=round(must_have_multiplier, 4),
        weighted_avg_base=round(weighted_avg_base, 2) if weighted_avg_base else None,
        type_scores=type_scores,
    )


def score_all_companies(user_id: str = "house") -> List[CompanyScorecard]:
    """
    Score every company in the DB. Returns list sorted by overall_score descending.
    Companies with NULL scores appear at the bottom.
    """
    companies = db.get_all_companies()
    scorecards = []
    for c in companies:
        try:
            sc = score_company(c["id"], user_id)
            scorecards.append(sc)
        except Exception as e:
            logger.error(f"Scoring failed for {c['name']}: {e}")

    # Sort: scored companies first (high to low), then incomplete ones
    scorecards.sort(
        key=lambda sc: (sc.overall_score is None, -(sc.overall_score or 0))
    )
    return scorecards


# ---------------------------------------------------------------------------
# Gap detection helper (used by gap_agent.py)
# ---------------------------------------------------------------------------

def get_missing_must_haves(company_id: str) -> List[Dict]:
    """
    Return all must-have metrics with no current value for a company.

    Returns a list of dicts with metric info including obtain_method,
    which determines whether the gap agent fires Type A (outreach) or
    Type B (web search).
    """
    latest_values = db.get_latest_values_for_company(company_id)
    all_metrics = db.get_all_metrics()

    # Build set of metric_ids that already have a value
    filled_metric_ids = {row["metric_id"] for row in latest_values if row.get("value")}

    missing = []
    for m in all_metrics:
        if m.get("must_have") and m["id"] not in filled_metric_ids:
            missing.append({
                "metric_id": m["id"],
                "metric_name": m["name"],
                "metric_type_name": (m.get("metric_type") or {}).get("name", ""),
                "obtain_method": m.get("obtain_method", "manual"),
                "description": m.get("description", ""),
            })
    return missing
