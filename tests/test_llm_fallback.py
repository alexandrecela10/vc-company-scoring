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

from pipeline.extractors.llm_fallback import LLMFallback
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
        # Chunk mentions MRR; LLM returns a plausible value + verbatim quote.
        chunks = [chunk("Financials slide: our monthly recurring revenue is $1,200,000 today.")]
        provider = FakeLLMProvider({
            "llm_fallback.mrr": json.dumps({
                "value": 1200000,
                "evidence_quote": "monthly recurring revenue is $1,200,000",
            }),
        })
        fallback = LLMFallback.from_yaml(RULES_PATH, provider=provider)
        obs = fallback.extract_missing(chunks, ["mrr"])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].metric_name, "mrr")
        self.assertEqual(obs[0].value, "1200000")
        self.assertEqual(obs[0].method, "llm")
        self.assertEqual(obs[0].chunk_locator, "slide_1")

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


if __name__ == "__main__":
    unittest.main()
