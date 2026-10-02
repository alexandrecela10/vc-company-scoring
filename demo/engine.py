"""In-memory scoring for the public demo. No database.

Reuses the real pipeline (preprocess, rules, optional LLM fallback) and the
real scorer. Only the storage layer is replaced: observations stay in memory
and one value per metric is picked with the same ladder as value_resolver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from pipeline.extractors.base import Observation
from pipeline.extractors.pitchdeck_v1 import run_pitchdeck_extraction
from pipeline.value_resolver import _SCENARIO_RANK
from scorer import MetricTypeScore, _score_metric_type

# Metric types from seed.sql (id, name, must_have, house_weight).
METRIC_TYPES = [
    {"id": "t_founders", "name": "Founders Strength", "must_have": True, "house_weight": 1.0},
    {"id": "t_moat", "name": "Technology Moat", "must_have": True, "house_weight": 1.0},
    {"id": "t_market", "name": "Market Growth", "must_have": True, "house_weight": 1.0},
    {"id": "t_competition", "name": "Competitive Landscape", "must_have": True, "house_weight": 1.0},
    {"id": "t_unit_econ", "name": "Unit Economics", "must_have": True, "house_weight": 1.0},
    {"id": "t_financials", "name": "Financials", "must_have": True, "house_weight": 1.0},
    {"id": "t_demographics", "name": "Company Demographics", "must_have": False, "house_weight": 0.3},
]

# The 12 metrics a deck can fill (rules.yaml keys), with their seed/migration definitions.
METRICS = {
    "prior_successful_exit": ("Prior Successful Exit", "boolean", 1.5, True, "t_founders"),
    "technical_cofounder": ("Technical Co-Founder", "boolean", 0.8, False, "t_founders"),
    "mrr": ("Monthly Recurring Revenue", "number", 1.0, False, "t_financials"),
    "funding_stage": ("Funding Stage", "score_1_5", 1.0, True, "t_financials"),
    "runway_months": ("Runway (months)", "number", 0.8, False, "t_financials"),
    "arr": ("Annual Recurring Revenue", "number", 1.0, False, "t_financials"),
    "revenue": ("Revenue", "number", 0.8, False, "t_financials"),
    "gross_margin": ("Gross Margin", "number", 0.8, False, "t_financials"),
    "burn_rate": ("Burn Rate", "number", 0.6, False, "t_financials"),
    "customer_count": ("Customer Count", "number", 0.5, False, "t_financials"),
    "employee_count": ("Employee Count", "number", 0.5, False, "t_demographics"),
    "founding_year": ("Founding Year", "number", 0.3, False, "t_demographics"),
}


@dataclass
class DeckResult:
    """Everything the UI shows for one deck."""
    name: str
    slides: int
    values: List[Observation]                     # one winning value per metric
    financials: Optional[MetricTypeScore]
    missing_metrics: List[str]                    # deck metrics not found
    missing_must_have_types: List[str]            # must-have categories with no score
    used_llm: bool = False
    error: Optional[str] = None
    all_observations: List[Observation] = field(default_factory=list)

    @property
    def rank_score(self) -> Optional[float]:
        """Deck-verifiable score used for ranking: the Financials type score."""
        return self.financials.type_score if self.financials else None


def _ladder(o: Observation):
    """Same order as value_resolver: scenario rank, then latest date (undated last), then confidence."""
    days = date.fromisoformat(o.as_of_date).toordinal() if o.as_of_date else 0
    return (_SCENARIO_RANK.get(o.scenario, _SCENARIO_RANK[None]), -days, -o.confidence)


def pick_winners(observations: List[Observation]) -> List[Observation]:
    """One value per metric, in catalog order."""
    best: Dict[str, Observation] = {}
    for o in sorted(observations, key=_ladder):
        best.setdefault(o.metric_name, o)
    return [best[c] for c in METRICS if c in best]


def score(values: List[Observation]):
    """Score each metric type with the real scorer. Returns (financials, missing must-have types)."""
    rows_by_type: Dict[str, List[Dict]] = {}
    for o in values:
        name, vtype, weight, must, type_id = METRICS[o.metric_name]
        rows_by_type.setdefault(type_id, []).append({
            "metric_id": o.metric_name,
            "value": o.value,
            "metric": {"name": name, "code": o.metric_name, "value_type": vtype,
                       "weight": weight, "must_have": must, "metric_type_id": type_id},
            "data_source": {"name": f"Deck, {o.chunk_locator.replace('_', ' ')}"},
            "raw_evidence": o.evidence_text,
            "confidence": o.confidence,
            "captured_by": o.method,
            "period_granularity": o.period_granularity,
        })
    type_scores = [_score_metric_type(t, rows_by_type.get(t["id"], [])) for t in METRIC_TYPES]
    financials = next(t for t in type_scores if t.metric_type_name == "Financials")
    missing = [t.metric_type_name for t in type_scores if t.must_have and t.type_score is None]
    return financials, missing


def analyse(name: str, raw: bytes, provider=None) -> DeckResult:
    """Run the real pipeline on one deck. provider=None means rules only (no LLM cost)."""
    r = run_pitchdeck_extraction(raw, "demo", dry_run=True, provider=provider,
                                 use_llm_fallback=provider is not None, origin_path=name)
    if r.error:
        return DeckResult(name, r.chunk_count, [], None, [], [], error=r.error)
    values = pick_winners(r.observations)
    financials, missing_types = score(values)
    return DeckResult(name, r.chunk_count, values, financials, r.missing_metrics, missing_types,
                      used_llm=provider is not None, all_observations=r.observations)


def rank(results: List[DeckResult]) -> List[DeckResult]:
    """Highest Financials score first; decks with no score go last."""
    return sorted(results, key=lambda d: (d.rank_score is None, -(d.rank_score or 0)))


def draft_ask(company: str, missing_metrics: List[str]) -> str:
    """Template founder email for missing deck metrics. No LLM, never sent."""
    items = "\n".join(f"- {METRICS[m][0]}" for m in missing_metrics if m in METRICS)
    return (f"Hi {company} team,\n\nThanks for the deck. Before our first call, could you share:\n"
            f"{items}\n\nA one-line answer or a slide reference is enough.\n\nThe investment team")
