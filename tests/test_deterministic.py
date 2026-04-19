"""Unit tests for the deterministic rule engine.

We hand-craft Chunks that simulate typical pitch-deck text snippets
and assert the engine returns the expected Observations.

Run with:  python3 -m unittest tests.test_deterministic
"""
from __future__ import annotations

import unittest
from pathlib import Path

from pipeline.extractors.deterministic import DeterministicEngine
from pipeline.preprocessors.base import Chunk

RULES_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "rules.yaml"


def chunk(text: str, locator: str = "slide_1", ordinal: int = 0) -> Chunk:
    """Small helper so the tests stay readable."""
    return Chunk(text=text, locator=locator, ordinal=ordinal, metadata={})


class DeterministicEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = DeterministicEngine.from_yaml(RULES_PATH)

    # ---- mrr -----------------------------------------------------------

    def test_mrr_with_unit_m(self):
        obs = self.engine.extract([chunk("Our $1.2M MRR grew 30% YoY.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "1200000")
        self.assertEqual(mrr[0].method, "regex")
        self.assertIn("1.2M MRR", mrr[0].evidence_text)

    def test_mrr_with_unit_k(self):
        obs = self.engine.extract([chunk("Reached $185k MRR in Q3.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "185000")

    def test_mrr_colon_form_no_unit(self):
        obs = self.engine.extract([chunk("MRR: 185000 (stable)")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "185000")

    def test_mrr_out_of_range_discarded(self):
        # $100B MRR > max validate 10B -> engine must discard
        obs = self.engine.extract([chunk("We have $100B MRR, trust us.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(mrr, [])

    # ---- employee_count ------------------------------------------------

    def test_employee_count_team_of(self):
        obs = self.engine.extract([chunk("Team of 35 engineers shipping weekly.")])
        ec = [o for o in obs if o.metric_name == "employee_count"]
        self.assertEqual(len(ec), 1)
        self.assertEqual(ec[0].value, "35")

    def test_employee_count_fulltime(self):
        obs = self.engine.extract([chunk("45 full-time employees across 3 offices.")])
        ec = [o for o in obs if o.metric_name == "employee_count"]
        self.assertEqual(len(ec), 1)
        self.assertEqual(ec[0].value, "45")

    # ---- founding_year -------------------------------------------------

    def test_founding_year(self):
        obs = self.engine.extract([chunk("Founded in 2019 by two ex-Stripe engineers.")])
        fy = [o for o in obs if o.metric_name == "founding_year"]
        self.assertEqual(len(fy), 1)
        self.assertEqual(fy[0].value, "2019")

    # ---- funding_stage (enum_map) --------------------------------------

    def test_funding_stage_series_a(self):
        obs = self.engine.extract([chunk("We closed our Series A led by Accel.")])
        fs = [o for o in obs if o.metric_name == "funding_stage"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].value, "3")
        self.assertEqual(fs[0].method, "enum_map")

    def test_funding_stage_preseed_not_seed(self):
        # 'pre-seed' must be recognised as 1, not 'seed' (2).
        # This depends on the map being ordered most-specific-first in the YAML.
        obs = self.engine.extract([chunk("Pre-seed round closed last month.")])
        fs = [o for o in obs if o.metric_name == "funding_stage"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].value, "1")

    # ---- prior_successful_exit (context-gated enum_map) ---------------

    def test_prior_exit_with_founder_context(self):
        text = "The founder previously sold to Google for $500M."
        obs = self.engine.extract([chunk(text)])
        pe = [o for o in obs if o.metric_name == "prior_successful_exit"]
        self.assertEqual(len(pe), 1)
        self.assertEqual(pe[0].value, "true")

    def test_prior_exit_without_founder_context_is_rejected(self):
        # No founder/ceo/previously keyword -> context gate fails -> no observation.
        text = "The team sold to Google last year."
        obs = self.engine.extract([chunk(text)])
        pe = [o for o in obs if o.metric_name == "prior_successful_exit"]
        self.assertEqual(pe, [])

    # ---- engine invariants --------------------------------------------

    def test_empty_chunk_yields_nothing(self):
        self.assertEqual(self.engine.extract([chunk("")]), [])

    def test_evidence_is_substring_of_chunk(self):
        # Grounding invariant: evidence must literally appear inside chunk.text.
        text = "Our $1.2M MRR grew 30% YoY."
        c = chunk(text)
        for obs in self.engine.extract([c]):
            self.assertIn(obs.evidence_text, c.text)

    def test_metric_code_filter(self):
        # Only extract `funding_stage`; MRR must be ignored even if present.
        text = "Series A round at $1.2M MRR."
        obs = self.engine.extract([chunk(text)], metric_codes=["funding_stage"])
        codes = {o.metric_name for o in obs}
        self.assertEqual(codes, {"funding_stage"})


if __name__ == "__main__":
    unittest.main()
