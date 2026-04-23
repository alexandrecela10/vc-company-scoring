"""Contracts shared by all extractors.

An Extractor's only job is: list[Chunk] -> list[Observation].
It does NOT:
  - persist observations to the DB (orchestrator's job)
  - decide which observation wins per (company, metric) (value_resolver's job)
  - call the entity resolver (orchestrator already knows company_id)

This separation keeps extractors pure, deterministic, and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol

# The five extraction methods. Kept as a Literal (not an enum) so rules.yaml
# can use plain strings and mypy still checks them.
ExtractionMethod = Literal["regex", "enum_map", "derived", "llm", "manual"]


@dataclass
class Observation:
    """A single candidate metric value extracted from one chunk.

    Grounding contract: every field here MUST be populated. If any is
    missing, the extractor returns nothing and the orchestrator creates a
    gap_action. We never persist an Observation with a NULL evidence_text
    or missing chunk_locator.
    """
    # --- identifies WHAT was extracted ---
    metric_name: str           # rules.yaml key, e.g. "mrr". Orchestrator maps to metric_id.
    value: str                 # canonical string form: "185000", "true", "3"

    # --- identifies WHERE it came from ---
    chunk_locator: str         # matches Chunk.locator, e.g. "slide_7"
    evidence_text: str         # literal quote, MUST be substring of chunk.text

    # --- identifies HOW it was extracted ---
    method: ExtractionMethod   # regex | enum_map | derived | llm | manual
    confidence: float          # 0.0-1.0, calibrated per method (see rules.yaml)

    # Free-form diagnostics (pattern_id, llm_prompt_hash, derived_from=[...]).
    # Stored on metric_observation.method_details JSONB for audit.
    method_details: Dict[str, Any] = field(default_factory=dict)

    # --- temporal contract (Phase 2c) ---
    # All optional -- legacy deterministic observations stay NULL-compatible.
    # Resolver uses `scenario` + `as_of_date` to pick actuals over projections
    # and newer actuals over older ones. rules.yaml `temporal.requires` can
    # force the LLM path to populate these; if missing, the observation is
    # rejected (prevents ambiguous facts from reaching the DB).
    as_of_date: Optional[str] = None          # ISO "YYYY-MM-DD" that the CLAIM describes
    period_granularity: Optional[str] = None  # point_in_time|month|quarter|year|trailing_12m
    scenario: Optional[str] = None            # actual|estimate|projection|forecast
    currency: Optional[str] = None            # ISO-4217 for money metrics
    period_label: Optional[str] = None        # raw label from source, verbatim (e.g. "2026E")


class Extractor(Protocol):
    """Structural interface every extractor must satisfy."""

    extractor_id: str          # UUID that matches the extractor DB table row
    version: str               # human-readable, e.g. "pitchdeck_v1"

    def extract(self, chunks: List["Chunk"]) -> List[Observation]:  # type: ignore[name-defined]
        """Scan chunks and return any observations the extractor can justify.

        Returning [] is valid and expected — e.g. a pitch deck with no
        financial slide yields no MRR observation. The orchestrator decides
        whether that warrants a gap_action.
        """
        ...
