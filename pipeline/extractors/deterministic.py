"""Deterministic rule engine: runs `regex` and `enum_map` methods from rules.yaml.

This is the core of Phase 2a extraction. It's intentionally boring:
  - No LLM calls (that's llm_fallback.py).
  - No DB writes (orchestrator's job).
  - No entity resolution (orchestrator already knows the company).

Invariants enforced on every Observation we emit:
  1. `evidence_text` is a LITERAL substring of the source chunk's text.
  2. At most ONE observation per (chunk, metric). First matching method wins,
     first match within that method wins.
  3. Numeric values pass the `validate` min/max range or we discard.

If rules.yaml is malformed, construction raises RuleError (fail fast).
At runtime, a bad chunk yields no observations -- we never crash upload.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from pipeline.extractors.base import Observation
from pipeline.preprocessors.base import Chunk

# Chars of surrounding text kept alongside the match, so the UI evidence
# panel shows enough context to be read without opening the whole slide.
# Still a substring of chunk.text -- we only slice, never concatenate.
EVIDENCE_CONTEXT_CHARS = 30


class RuleError(ValueError):
    """Raised at engine construction if rules.yaml is malformed."""


class DeterministicEngine:
    """Reads rules.yaml once, pre-compiles regexes, runs against Chunks."""

    def __init__(self, rules: Dict[str, Dict[str, Any]]):
        # _rules is a dict: metric_code -> {value_type, validate, compiled_methods}
        # Each compiled_method has the original YAML fields PLUS pre-compiled
        # regex objects so we don't re-compile on every chunk.
        self._rules = self._validate_and_compile(rules)

    # --- construction ----------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "DeterministicEngine":
        """Load rules.yaml from disk and build an engine."""
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise RuleError(f"{path}: top-level must be a mapping of metric_code -> config")
        return cls(raw)

    def _validate_and_compile(
        self, rules: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Check shape, compile regexes, freeze the config."""
        compiled: Dict[str, Dict[str, Any]] = {}
        for code, cfg in rules.items():
            if not isinstance(cfg, dict) or "methods" not in cfg:
                raise RuleError(f"{code}: missing `methods` list")

            methods: List[Dict[str, Any]] = []
            for idx, m in enumerate(cfg["methods"]):
                mtype = m.get("method")

                if mtype == "regex":
                    # Pre-compile every pattern with IGNORECASE; catch bad patterns
                    # here rather than mid-upload.
                    try:
                        compiled_pats = [re.compile(p, re.IGNORECASE) for p in m["patterns"]]
                    except re.error as e:
                        raise RuleError(f"{code}[{idx}] regex compile failed: {e}") from e
                    methods.append({**m, "compiled_patterns": compiled_pats})

                elif mtype == "enum_map":
                    # For each keyword in the map, pre-compile a word-boundary regex.
                    # `\b` avoids "series axolotl" matching "series a".
                    kw_patterns = [
                        (re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), v)
                        for k, v in m["map"].items()
                    ]
                    # Optional context-gate: chunk must contain >= 1 `any_of` keyword.
                    ctx_patterns: Optional[List[re.Pattern]] = None
                    ctx = m.get("require_context") or {}
                    if ctx.get("any_of"):
                        ctx_patterns = [
                            re.compile(rf"\b{re.escape(c)}\b", re.IGNORECASE)
                            for c in ctx["any_of"]
                        ]
                    methods.append(
                        {**m, "kw_patterns": kw_patterns, "context_patterns": ctx_patterns}
                    )

                elif mtype == "llm":
                    # Skipped here. Kept in the list so llm_fallback.py can read
                    # the same rules.yaml without needing a separate file.
                    methods.append(m)

                else:
                    raise RuleError(f"{code}[{idx}]: unknown method type {mtype!r}")

            compiled[code] = {**cfg, "compiled_methods": methods}
        return compiled

    # --- public API ------------------------------------------------------

    def extract(
        self, chunks: List[Chunk], metric_codes: Optional[List[str]] = None
    ) -> List[Observation]:
        """Scan every chunk for every configured metric.

        `metric_codes` lets callers limit extraction to a subset
        (e.g. re-running just MRR after tweaking its regex).
        """
        filtered = (
            self._rules if metric_codes is None
            else {k: v for k, v in self._rules.items() if k in metric_codes}
        )
        results: List[Observation] = []
        for chunk in chunks:
            for code, cfg in filtered.items():
                obs = self._extract_one(code, cfg, chunk)
                if obs is not None:
                    results.append(obs)
        return results

    # --- per-metric dispatch --------------------------------------------

    def _extract_one(
        self, code: str, cfg: Dict[str, Any], chunk: Chunk
    ) -> Optional[Observation]:
        """Try each method in order; return the first Observation, or None."""
        for m in cfg["compiled_methods"]:
            mtype = m["method"]
            if mtype == "regex":
                obs = self._try_regex(code, cfg, m, chunk)
            elif mtype == "enum_map":
                obs = self._try_enum_map(code, cfg, m, chunk)
            else:
                # llm and anything else: skipped by this engine.
                continue
            if obs is not None:
                return obs
        return None

    # --- method implementations -----------------------------------------

    def _try_regex(
        self, code: str, cfg: Dict[str, Any], m: Dict[str, Any], chunk: Chunk
    ) -> Optional[Observation]:
        for pat in m["compiled_patterns"]:
            match = pat.search(chunk.text)
            if not match:
                continue

            # Turn the regex capture groups into a canonical value.
            try:
                value = self._normalise(m.get("normalise", {}), match)
            except (ValueError, KeyError, IndexError):
                # Malformed number like "1..2" -> skip this match, try next pattern.
                continue
            if value is None:
                continue

            # Out-of-range -> treat as a bad match (e.g. matched a zip code as year).
            if not self._within_range(value, cfg.get("validate")):
                continue

            evidence = self._context_around(chunk.text, match.start(), match.end())
            assert evidence in chunk.text, "evidence must be substring of chunk.text"

            return Observation(
                metric_name=code,
                value=self._value_to_string(value, cfg.get("value_type")),
                chunk_locator=chunk.locator,
                evidence_text=evidence,
                method="regex",
                confidence=float(m["confidence"]),
                method_details={
                    "pattern": pat.pattern,
                    "match": match.group(0),
                    "normalised": value,
                },
            )
        return None

    def _try_enum_map(
        self, code: str, cfg: Dict[str, Any], m: Dict[str, Any], chunk: Chunk
    ) -> Optional[Observation]:
        # Context gate: if required, chunk must contain at least one context keyword.
        # Used e.g. by prior_successful_exit to require "founder"/"previously" etc.
        if m["context_patterns"] is not None:
            if not any(p.search(chunk.text) for p in m["context_patterns"]):
                return None

        # Scan every keyword; pick the earliest occurrence (deterministic order).
        best: Optional[tuple] = None  # (start, end, matched_text, value, pattern)
        for kw_pat, kw_value in m["kw_patterns"]:
            match = kw_pat.search(chunk.text)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), match.end(), match.group(0), kw_value, kw_pat.pattern)
        if best is None:
            return None

        start, end, matched, value, pattern = best
        evidence = self._context_around(chunk.text, start, end)
        assert evidence in chunk.text, "evidence must be substring of chunk.text"

        return Observation(
            metric_name=code,
            value=self._value_to_string(value, cfg.get("value_type")),
            chunk_locator=chunk.locator,
            evidence_text=evidence,
            method="enum_map",
            confidence=float(m["confidence"]),
            method_details={"keyword": matched, "pattern": pattern},
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _normalise(norm_cfg: Dict[str, Any], match: re.Match) -> Optional[float]:
        """Turn regex capture groups into a canonical numeric value.

        Supported kinds:
          integer           -> int(amount), commas stripped
          number_with_unit  -> float(amount) * units[unit_group], unit optional
        """
        kind = norm_cfg.get("kind")
        if kind == "integer":
            amt = match.group(norm_cfg["amount_group"]).replace(",", "")
            # Use float() then int() so "35.0" also works; ValueError caught upstream.
            return int(float(amt))

        if kind == "number_with_unit":
            amt_str = match.group(norm_cfg["amount_group"]).replace(",", "")
            amount = float(amt_str)
            unit_group = norm_cfg.get("unit_group")
            # The unit group may not have matched (e.g. "MRR: 185000" has no unit).
            unit_val = match.group(unit_group) if unit_group else None
            units_map = norm_cfg.get("units", {})
            mult = float(units_map.get(unit_val.lower(), 1)) if unit_val else 1.0
            return amount * mult

        # No normalise block -> caller shouldn't have called us, but be graceful.
        return None

    @staticmethod
    def _within_range(value: float, validate_cfg: Optional[Dict[str, Any]]) -> bool:
        """True if `value` is inside validate.min / validate.max (both optional)."""
        if not validate_cfg:
            return True
        if "min" in validate_cfg and value < validate_cfg["min"]:
            return False
        if "max" in validate_cfg and value > validate_cfg["max"]:
            return False
        return True

    @staticmethod
    def _value_to_string(value: Any, value_type: Optional[str]) -> str:
        """Canonical string form for company_metric_value.value.

        - boolean   -> "true" | "false"
        - number    -> "185000" or "1.5" (prefer integer representation when exact)
        - score_1_5 -> "1".."5"
        """
        if value_type == "boolean":
            return "true" if bool(value) else "false"
        if value_type == "score_1_5":
            return str(int(value))
        if value_type == "number":
            if isinstance(value, (int, float)) and float(value).is_integer():
                return str(int(value))
            return str(value)
        return str(value)

    @staticmethod
    def _context_around(
        text: str, start: int, end: int, window: int = EVIDENCE_CONTEXT_CHARS
    ) -> str:
        """Slice `text[start-window : end+window]`, clamped to chunk bounds.

        We slice (never concatenate) so the result is guaranteed to be a
        literal substring of `text` -- the grounding invariant.
        """
        lo = max(0, start - window)
        hi = min(len(text), end + window)
        return text[lo:hi]
