"""Unit tests for LLMFallback using a FakeLLMProvider.

No network calls. We verify that the PARSING + QUOTE VERIFICATION +
RANGE VALIDATION logic behaves correctly regardless of which LLM we
plug in later.

Run:  python3 -m unittest tests.test_llm_fallback
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from datetime import date

from pipeline.extractors.llm_fallback import (
    LLMFallback,
    _derive_scenario_and_date,
)
from pipeline.preprocessors.base import Chunk

RULES_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "rules.yaml"


class FakeLLMProvider:
    """Minimal LLMProvider that returns a pre-scripted response per call.

    We script one response per metric (indexed by the span_name argument
    the fallback passes, like 'llm_fallback.mrr').
    """

    def __init__(self, responses: Dict[str, str]):
        self.responses = responses
        self.name = "fake"
        self.calls: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        span_name: str = "llm_fallback",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self.calls.append(span_name)
        # Default response if nothing scripted for this metric.
        return self.responses.get(
            span_name, json.dumps({"value": None, "evidence_quote": None})
        )


def chunk(text: str, locator: str = "slide_1", ordinal: int = 0) -> Chunk:
    return Chunk(text=text, locator=locator, ordinal=ordinal, metadata={})


class LLMFallbackTests(unittest.TestCase):
    # ---- happy path: valid quote, value in range ----------------------

    def test_valid_response_produces_observation(self):
        # Chunk mentions MRR + a period label; LLM now must return period_label
        # because rules.yaml declares `temporal.requires: [as_of_date]` for MRR.
        chunks = [chunk(
            "Financials slide Dec 2024: our monthly recurring revenue is $1,200,000."
        )]
        provider = FakeLLMProvider({
            "llm_fallback.mrr": json.dumps({
                "value": 1200000,
                "evidence_quote": "monthly recurring revenue is $1,200,000",
                "period_label": "Dec 2024",
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["mrr"])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].metric_name, "mrr")
        self.assertEqual(obs[0].value, "1200000")
        self.assertEqual(obs[0].method, "llm")
        self.assertEqual(obs[0].chunk_locator, "slide_1")
        # Temporal contract populated from "Dec 2024".
        self.assertEqual(obs[0].scenario, "actual")
        self.assertEqual(obs[0].as_of_date, "2024-12-31")
        self.assertEqual(obs[0].period_label, "Dec 2024")
        self.assertEqual(obs[0].currency, "USD")

    # ---- G2: quote not a verbatim substring -> rejected --------------

    def test_paraphrased_quote_is_rejected(self):
        chunks = [chunk("Financials slide: MRR of $1,200,000 today.")]
        # LLM paraphrases (adds a word that's not in the chunk).
        provider = FakeLLMProvider({
            "llm_fallback.mrr": json.dumps({
                "value": 1200000,
                "evidence_quote": "MRR stands at $1,200,000 today",  # NOT in chunk
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["mrr"])
        self.assertEqual(obs, [])

    # ---- G3: value out of range -> rejected --------------------------

    def test_out_of_range_value_is_rejected(self):
        chunks = [chunk("MRR is 99,999,999,999 usd per month.")]
        # 99B > rules.yaml max (10B)
        provider = FakeLLMProvider({
            "llm_fallback.mrr": json.dumps({
                "value": 99_999_999_999,
                "evidence_quote": "MRR is 99,999,999,999 usd per month",
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["mrr"])
        self.assertEqual(obs, [])

    # ---- G4: null value -> no observation, no error -------------------

    def test_null_value_produces_no_observation(self):
        chunks = [chunk("Our MRR growth is strong.")]
        provider = FakeLLMProvider({
            "llm_fallback.mrr": json.dumps({
                "value": None,
                "evidence_quote": None,
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        self.assertEqual(fallback.extract_missing(chunks, ["mrr"]), [])

    # ---- G1: non-JSON response -> rejected ---------------------------

    def test_non_json_response_is_rejected(self):
        chunks = [chunk("MRR is stated somewhere.")]
        provider = FakeLLMProvider({"llm_fallback.mrr": "I think it's $1.2M."})
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        self.assertEqual(fallback.extract_missing(chunks, ["mrr"]), [])

    # ---- retrieval short-circuit: no chunk mentions topic -> no call -

    def test_no_matching_chunks_skips_llm_call(self):
        # Chunk has no MRR-related keywords; retrieval returns [].
        chunks = [chunk("We love dogs. They bark. They play fetch.")]
        provider = FakeLLMProvider({})  # would return null anyway
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["mrr"])
        self.assertEqual(obs, [])
        # Crucially: no call was made to the provider.
        self.assertEqual(provider.calls, [])

    # ---- boolean metric + LLM ----------------------------------------

    def test_boolean_true_with_verbatim_quote(self):
        chunks = [chunk("The founder previously sold her startup Weave to Mastercard.")]
        provider = FakeLLMProvider({
            "llm_fallback.prior_successful_exit": json.dumps({
                "value": True,
                "evidence_quote": "founder previously sold her startup Weave to Mastercard",
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["prior_successful_exit"])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].value, "true")


class TemporalDerivationTests(unittest.TestCase):
    """_derive_scenario_and_date: pure-function tests, no LLM / IO."""

    def test_year_label_no_suffix_is_actual(self):
        # "2024" -> actual, year-end date.
        self.assertEqual(
            _derive_scenario_and_date("2024", "year"),
            ("actual", "2024-12-31"),
        )

    def test_year_label_e_suffix_is_estimate(self):
        self.assertEqual(
            _derive_scenario_and_date("2026E", "year"),
            ("estimate", "2026-12-31"),
        )

    def test_year_label_p_suffix_is_projection(self):
        self.assertEqual(
            _derive_scenario_and_date("2028P", "year"),
            ("projection", "2028-12-31"),
        )

    def test_quarter_label(self):
        # "Q4 2024" -> actual, Dec 31 of that year.
        self.assertEqual(
            _derive_scenario_and_date("Q4 2024", "quarter"),
            ("actual", "2024-12-31"),
        )

    def test_month_label(self):
        self.assertEqual(
            _derive_scenario_and_date("Dec 2024", "month"),
            ("actual", "2024-12-31"),
        )

    def test_unparseable_label_yields_nulls(self):
        # Pure garbage -> cannot derive; caller will reject if required.
        self.assertEqual(_derive_scenario_and_date("recent", "year"), (None, None))
        self.assertEqual(_derive_scenario_and_date(None, "year"), (None, None))
        self.assertEqual(_derive_scenario_and_date("", "year"), (None, None))

    # --- future-date downgrade (bug reported 2026-04-23) ----------------

    def test_future_year_without_suffix_becomes_projection(self):
        # "2028" with no E/P/F on a deck read in 2026 describes the future,
        # so it CANNOT be an actual -- should auto-downgrade to projection.
        self.assertEqual(
            _derive_scenario_and_date("2028", "year", today=date(2026, 4, 23)),
            ("projection", "2028-12-31"),
        )

    def test_future_quarter_without_suffix_becomes_projection(self):
        # Same logic at quarter granularity (the employee_count=12 case).
        self.assertEqual(
            _derive_scenario_and_date("Q3 2027", "quarter", today=date(2026, 4, 23)),
            ("projection", "2027-09-30"),
        )

    def test_past_year_without_suffix_stays_actual(self):
        # "2024" on a 2026 deck is a recorded past fact -> stays actual.
        self.assertEqual(
            _derive_scenario_and_date("2024", "year", today=date(2026, 4, 23)),
            ("actual", "2024-12-31"),
        )

    def test_explicit_suffix_beats_future_check(self):
        # "2028P" is ALREADY labelled projection -- we trust the author,
        # don't second-guess. (Symmetric: "2024P" is a retrospective
        # projection statement; still projection.)
        self.assertEqual(
            _derive_scenario_and_date("2028P", "year", today=date(2026, 4, 23)),
            ("projection", "2028-12-31"),
        )
        self.assertEqual(
            _derive_scenario_and_date("2028E", "year", today=date(2026, 4, 23)),
            ("estimate", "2028-12-31"),
        )


class TemporalContractEnforcementTests(unittest.TestCase):
    """Observation rejection when rules.yaml `temporal.requires` isn't met."""

    def test_missing_period_label_rejects_when_required(self):
        # ARR requires [as_of_date, scenario]. LLM returns no period_label ->
        # _derive returns (None, None) -> both required fields missing -> reject.
        chunks = [chunk(
            "Our annual recurring revenue is $2,400,000 on the financials slide."
        )]
        provider = FakeLLMProvider({
            "llm_fallback.arr": json.dumps({
                "value": 2_400_000,
                "evidence_quote": "annual recurring revenue is $2,400,000",
                "period_label": None,     # explicitly null
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["arr"])
        self.assertEqual(obs, [])

    def test_arr_with_projection_label_persists_as_projection(self):
        # Projections should STILL be captured (they're valid observations);
        # it's the resolver's job to prefer actuals. Contract only rejects
        # UNDATED observations.
        chunks = [chunk("Path to $52M ARR by 2028. Financials: 52.0 in 2028P.")]
        provider = FakeLLMProvider({
            "llm_fallback.arr": json.dumps({
                "value": 52_000_000,
                "evidence_quote": "52.0 in 2028P",
                "period_label": "2028P",
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["arr"])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].scenario, "projection")
        self.assertEqual(obs[0].as_of_date, "2028-12-31")
        self.assertEqual(obs[0].period_label, "2028P")


if __name__ == "__main__":
    unittest.main()
