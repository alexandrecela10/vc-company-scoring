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
    # NOTE: mrr declares `temporal.requires: [as_of_date]` in rules.yaml,
    # so every MRR test MUST include a nearby period label (year, "Q4 2024",
    # "Dec 2024") or the contract rejects the match. That's realistic: a
    # deck that mentions "$1.2M MRR" without any date context is ambiguous
    # and the extractor refuses to silently persist it.

    def test_mrr_with_unit_m(self):
        obs = self.engine.extract([chunk("Our $1.2M MRR in 2024 grew 30% YoY.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "1200000")
        self.assertEqual(mrr[0].method, "regex")
        self.assertIn("1.2M MRR", mrr[0].evidence_text)
        # Temporal enrichment should now be populated from "2024".
        self.assertEqual(mrr[0].scenario, "actual")
        self.assertEqual(mrr[0].as_of_date, "2024-12-31")
        self.assertEqual(mrr[0].period_label, "2024")

    def test_mrr_with_unit_k(self):
        obs = self.engine.extract([chunk("Reached $185k MRR in Q3 2024.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "185000")
        # "Q3 2024" -> end of Q3 = Sep 30.
        self.assertEqual(mrr[0].as_of_date, "2024-09-30")

    def test_mrr_colon_form_no_unit(self):
        obs = self.engine.extract([chunk("MRR: 185000 (Dec 2024, stable)")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(len(mrr), 1)
        self.assertEqual(mrr[0].value, "185000")
        self.assertEqual(mrr[0].period_label, "Dec 2024")

    def test_mrr_without_period_is_rejected(self):
        # Contract enforcement: mrr requires as_of_date. No year -> reject.
        obs = self.engine.extract([chunk("Our $1.2M MRR grew 30% YoY.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(mrr, [])

    def test_mrr_out_of_range_discarded(self):
        # $100B MRR > max validate 10B -> engine must discard (before temporal).
        obs = self.engine.extract([chunk("We have $100B MRR in 2024, trust us.")])
        mrr = [o for o in obs if o.metric_name == "mrr"]
        self.assertEqual(mrr, [])

    # ---- employee_count ------------------------------------------------
    # Same contract: employee_count requires as_of_date.

    def test_employee_count_team_of(self):
        obs = self.engine.extract([chunk("Team of 35 engineers shipping weekly in 2024.")])
        ec = [o for o in obs if o.metric_name == "employee_count"]
        self.assertEqual(len(ec), 1)
        self.assertEqual(ec[0].value, "35")

    def test_employee_count_fulltime(self):
        obs = self.engine.extract([chunk("45 full-time employees across 3 offices (2024).")])
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

    def test_revenue_year_label_not_read_as_amount(self):
        # Bug fix 2026-09-28: "Revenue 2025: $1.8M" was stored as 2025.
        obs = self.engine.extract([chunk("Revenue 2025: $1.8M")])
        rev = [o for o in obs if o.metric_name == "revenue"]
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0].value, "1800000")

    def test_funding_stage_does_not_capture_nearby_year(self):
        # Bug fix 2026-04-23: timeless metrics (requires=[]) must NEVER pick
        # up an opportunistic year from the scan window. A deck saying
        # "Series A close Q2 2026" should produce funding_stage=3 with NULL
        # temporals -- not tag it as_of=Q2 2026.
        obs = self.engine.extract([
            chunk("We closed our Series A led by Accel in Q2 2026.")
        ])
        fs = [o for o in obs if o.metric_name == "funding_stage"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].value, "3")
        self.assertIsNone(fs[0].as_of_date)
        self.assertIsNone(fs[0].scenario)
        self.assertIsNone(fs[0].period_label)

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
