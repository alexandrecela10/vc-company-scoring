"""LLM fallback: runs ONLY on metrics the deterministic engine missed.

Pipeline contract (enforced by the orchestrator in pitchdeck_v1.py):
    1. DeterministicEngine extracts what it can from chunks.
    2. Orchestrator computes missing_codes = metrics with no deterministic hit.
    3. LLMFallback.extract_missing(chunks, missing_codes) -> Observations.

Grounding guardrails (all applied AFTER the LLM returns):
    G1. Response must parse as JSON with keys {"value", "evidence_quote"}.
    G2. evidence_quote must be a VERBATIM SUBSTRING of one of the retrieved
        chunks. If not, observation rejected (no row written).
    G3. value must pass rules.yaml `validate.{min,max}` and the value_type.
    G4. value == null is a valid "LLM doesn't know" signal -> no observation
        (NOT a gap_action; the orchestrator decides that).

If ANY guardrail fails, we return None for that metric. Silent hallucinations
are worse than visible gaps.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from pipeline.extractors.base import Observation
from pipeline.extractors.llm_provider import LLMProvider
from pipeline.preprocessors.base import Chunk

logger = logging.getLogger(__name__)

# Very small stopword set for keyword retrieval -- just enough to stop
# ubiquitous words from dominating the overlap score. Not a real NLP step;
# we're narrowing 30 chunks to 3, not searching the web.
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on",
    "is", "are", "was", "were", "be", "by", "at", "as", "with", "from",
}

# How many extracted words per retrieval_query we consider (keeps it cheap).
_MIN_TOKEN_LEN = 3

# Default max context chunks sent to the LLM per metric.
# Per-metric override via rules.yaml `max_chunks`.
_DEFAULT_MAX_CHUNKS = 3


# Period-label derivation lives in pipeline.extractors.temporal so the
# deterministic engine can import the same helpers. Re-exported as a private
# alias to preserve the existing test import (`_derive_scenario_and_date`).
from pipeline.extractors.temporal import (  # noqa: E402 (placed after top imports on purpose)
    derive_scenario_and_date as _derive_scenario_and_date,
)


class LLMFallback:
    """Reads rules.yaml, runs one LLM call per missing metric.

    Decoupled from any specific provider (Gemini, Claude, ...): pass an
    LLMProvider at construction and the fallback doesn't care which model.
    """

    def __init__(self, rules: Dict[str, Dict[str, Any]], provider: LLMProvider):
        # Same rules dict as DeterministicEngine; we only care about `llm` methods.
        self._rules = rules
        self._provider = provider

    @classmethod
    def from_yaml(
        cls, path: Union[str, Path], provider: LLMProvider
    ) -> "LLMFallback":
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top-level must be a mapping")
        return cls(raw, provider)

    # --- public API ------------------------------------------------------

    def extract_missing(
        self, chunks: List[Chunk], missing_codes: List[str]
    ) -> List[Observation]:
        """For each missing metric, run retrieval + LLM + verify.

        Returns observations only for metrics where every guardrail passed.
        """
        results: List[Observation] = []
        for code in missing_codes:
            cfg = self._rules.get(code)
            if cfg is None:
                # Metric listed as missing but absent from rules.yaml -- skip.
                continue

            # Find the `llm` method block for this metric (zero or one expected).
            llm_method = next(
                (m for m in cfg.get("methods", []) if m.get("method") == "llm"),
                None,
            )
            if llm_method is None:
                continue  # no fallback configured -> silent skip

            obs = self._extract_one(code, cfg, llm_method, chunks)
            if obs is not None:
                results.append(obs)
        return results

    # --- per-metric pipeline --------------------------------------------

    def _extract_one(
        self,
        code: str,
        cfg: Dict[str, Any],
        method: Dict[str, Any],
        chunks: List[Chunk],
    ) -> Optional[Observation]:
        # --- Step 1: retrieve relevant chunks by keyword overlap ---
        top = self._retrieve(
            chunks,
            method.get("retrieval_query", ""),
            k=method.get("max_chunks", _DEFAULT_MAX_CHUNKS),
        )
        if not top:
            # No chunk even mentions the topic keywords -> skip LLM call.
            logger.debug(f"LLM skipped for {code!r}: no chunks matched retrieval keywords")
            return None

        # --- Step 2: call the provider ---
        prompt = self._build_prompt(code, cfg, top)
        try:
            raw = self._provider.complete(prompt, span_name=f"llm_fallback.{code}")
        except Exception as e:
            # Network hiccup, rate limit, bad API key -> degrade gracefully.
            logger.warning(f"LLM call failed for {code}: {e}")
            return None

        # --- Step 3: parse JSON (G1) ---
        parsed = self._parse_json(raw)
        if parsed is None:
            return None
        value = parsed.get("value")
        quote = parsed.get("evidence_quote")

        # Explicit null -> "LLM doesn't know", no observation (G4).
        if value is None or quote is None:
            logger.debug(f"LLM returned null value/quote for {code!r} (model said 'don't know')")
            return None

        # --- Step 4: verify quote is verbatim substring of one chunk (G2) ---
        source_chunk = next((c for c in top if quote in c.text), None)
        if source_chunk is None:
            logger.warning(
                f"LLM quote for {code!r} not found in retrieved chunks; rejected"
            )
            return None

        # --- Step 5: type + range validation (G3) ---
        canonical = self._canonicalise(value, cfg)
        if canonical is None:
            logger.debug(
                f"LLM value {value!r} for {code!r} failed canonicalisation "
                f"(expected type {cfg.get('value_type')!r})"
            )
            return None
        if not self._within_range(canonical, cfg.get("validate")):
            logger.debug(
                f"LLM value {canonical!r} for {code!r} outside allowed range "
                f"{cfg.get('validate')!r}"
            )
            return None

        # --- Step 6: temporal contract (Phase 2c) -----------------------
        # If rules.yaml declares `temporal.requires`, the LLM must have
        # provided enough context (via period_label) for us to derive
        # scenario + as_of_date. Missing required fields -> rejection.
        # We derive deterministically from period_label so the LLM never
        # picks the scenario itself (keeps judgment out of the model).
        temporal_cfg = cfg.get("temporal") or {}
        period_label = parsed.get("period_label")
        if isinstance(period_label, str):
            period_label = period_label.strip() or None
        else:
            period_label = None

        scenario, as_of_date = _derive_scenario_and_date(
            period_label, temporal_cfg.get("period_granularity"),
        )

        required: List[str] = list(temporal_cfg.get("requires") or [])
        derived = {
            "as_of_date": as_of_date,
            "scenario": scenario,
            "period_label": period_label,
        }
        missing_required = [f for f in required if not derived.get(f)]
        if missing_required:
            logger.debug(
                f"LLM {code!r} missing required temporal fields "
                f"{missing_required} (period_label={period_label!r}); rejected"
            )
            return None

        return Observation(
            metric_name=code,
            value=self._value_to_string(canonical, cfg.get("value_type")),
            chunk_locator=source_chunk.locator,
            evidence_text=quote,
            method="llm",
            confidence=float(method["confidence"]),
            method_details={
                "provider": getattr(self._provider, "name", "unknown"),
                "retrieval_query": method.get("retrieval_query", ""),
                "retrieved_chunks": [c.locator for c in top],
                "raw_llm_value": value,
            },
            as_of_date=as_of_date,
            period_granularity=temporal_cfg.get("period_granularity"),
            scenario=scenario,
            currency="USD" if cfg.get("unit") == "usd" else None,
            period_label=period_label,
        )

    # --- retrieval ------------------------------------------------------

    def _retrieve(self, chunks: List[Chunk], query: str, k: int) -> List[Chunk]:
        """Top-k chunks by count of query tokens appearing in chunk.text.

        - Word-boundary match (so "mrr" doesn't match "commerce").
        - Lowercased on both sides.
        - Chunks with score 0 are excluded entirely. Returning [] here
          short-circuits the whole metric (saves an LLM call).
        """
        tokens = {
            t
            for t in re.split(r"\W+", query.lower())
            if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
        }
        if not tokens:
            return []

        scored: List[tuple[int, Chunk]] = []
        for c in chunks:
            lower = c.text.lower()
            score = sum(
                1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", lower)
            )
            if score > 0:
                scored.append((score, c))

        # Sort by score desc, then by ordinal asc for stable output.
        scored.sort(key=lambda x: (-x[0], x[1].ordinal))
        return [c for _, c in scored[:k]]

    # --- prompt ---------------------------------------------------------

    def _build_prompt(
        self, code: str, cfg: Dict[str, Any], chunks: List[Chunk]
    ) -> str:
        value_type = cfg.get("value_type", "number")
        rubric = _VALUE_TYPE_RUBRIC.get(value_type, "")
        context = "\n\n".join(f"[{c.locator}] {c.text}" for c in chunks)

        # Temporal block: only included when rules.yaml declares a non-empty
        # `temporal.requires`. Keeps prompts for timeless metrics (founding_year,
        # prior_successful_exit) short and focused.
        temporal_cfg = cfg.get("temporal") or {}
        if temporal_cfg.get("requires"):
            temporal_block = _TEMPORAL_INSTRUCTIONS.format(
                granularity=temporal_cfg.get("period_granularity", "year"),
            )
            json_keys = (
                '  "value"          -- the extracted value (see rubric), or null.\n'
                '  "evidence_quote" -- verbatim substring from a context chunk, or null.\n'
                '  "period_label"   -- verbatim column header / date phrase from the source\n'
                '                      that pinpoints WHEN this value applies, or null.'
            )
        else:
            temporal_block = ""
            json_keys = (
                '  "value"          -- the extracted value (see rubric), or null.\n'
                '  "evidence_quote" -- verbatim substring from a context chunk, or null.'
            )

        return _PROMPT_TEMPLATE.format(
            metric=code,
            value_type=value_type,
            rubric=rubric,
            temporal_block=temporal_block,
            json_keys=json_keys,
            context=context,
        )

    # --- parsing + canonicalisation ------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
        """Strict JSON parse. Strips ```json fences if present."""
        text = raw.strip()
        # Allow ```json ... ``` wrappers since Gemini occasionally adds them.
        if text.startswith("```"):
            inner = text.split("```", 2)
            if len(inner) >= 2:
                candidate = inner[1]
                if candidate.startswith("json"):
                    candidate = candidate[4:]
                text = candidate.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"LLM returned non-JSON: {raw[:120]!r}")
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _canonicalise(value: Any, cfg: Dict[str, Any]) -> Optional[float]:
        """Coerce the LLM's value into a type-correct numeric form.

        Returns None if the coercion is impossible (wrong type entirely).
        Boolean values are returned as 1.0 / 0.0 for range checks to be
        trivially True.
        """
        vt = cfg.get("value_type")
        try:
            if vt == "boolean":
                # Accept true/false, "true"/"false", 1/0.
                if isinstance(value, bool):
                    return 1.0 if value else 0.0
                if isinstance(value, str):
                    v = value.strip().lower()
                    if v in ("true", "yes"):
                        return 1.0
                    if v in ("false", "no"):
                        return 0.0
                    return None
                if isinstance(value, (int, float)):
                    return 1.0 if value else 0.0
                return None
            if vt == "score_1_5":
                n = int(value)
                return float(n)
            # number (default)
            if isinstance(value, bool):
                return None  # don't let `true` slip into a numeric metric
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _within_range(value: float, validate_cfg: Optional[Dict[str, Any]]) -> bool:
        if not validate_cfg:
            return True
        if "min" in validate_cfg and value < validate_cfg["min"]:
            return False
        if "max" in validate_cfg and value > validate_cfg["max"]:
            return False
        return True

    @staticmethod
    def _value_to_string(value: float, value_type: Optional[str]) -> str:
        if value_type == "boolean":
            return "true" if value >= 0.5 else "false"
        if value_type == "score_1_5":
            return str(int(value))
        if value_type == "number":
            if float(value).is_integer():
                return str(int(value))
            return str(value)
        return str(value)


# ---------------------------------------------------------------------------
# Prompt templates -- kept at module level so tests can reference them.
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """You are a strict evidence extractor for a VC company database.

Metric: {metric}
Value type: {value_type}
{rubric}
{temporal_block}
Task: From the context chunks below, find the company's {metric}.
Respond with JSON ONLY (no prose, no markdown fences) with these keys:
{json_keys}

Rules:
- Do NOT paraphrase. Copy any quote / label character-for-character from context.
- Do NOT guess. Absence of evidence = all values null.
- If the context mentions a value but not for THIS company, return nulls.

Context:
{context}

JSON response:"""


# Included in the prompt only when the metric declares `temporal.requires`.
# The model never chooses `scenario` itself -- our code derives it from the
# period_label suffix (E/P -> estimate/projection, plain year -> actual).
_TEMPORAL_INSTRUCTIONS = """
Temporal context: this metric is a {granularity}-granularity number. Decks
often show multiple periods side-by-side (e.g. 2024 | 2025 | 2026E | 2027P).
You MUST identify which period the value belongs to and return its verbatim
label in `period_label` (e.g. "2024", "2026E", "Q4 2024", "Dec 2024").
If you cannot pinpoint the period from the context, return all fields null.
Prefer the LATEST actual (no suffix) over estimates (E) over projections (P).
"""


_VALUE_TYPE_RUBRIC = {
    "number": "Rubric: Return a JSON number (no units, no currency symbols). "
              "For scaled quantities like '$1.2M', convert to 1200000.",
    "boolean": "Rubric: Return JSON true or false. Use null only if the context "
               "is completely silent on the question.",
    "score_1_5": "Rubric: Return an integer 1, 2, 3, 4, or 5 per the metric's "
                 "documented mapping.",
}
