"""Tests for the public demo: ranking on sample decks and the LLM cost guards."""
import tempfile
import unittest
from pathlib import Path

from demo.engine import analyse, pick_winners, rank
from demo.guard import BudgetExhausted, BudgetedProvider, DailyBudget, check_upload
from pipeline.extractors.base import Observation

SAMPLES = Path(__file__).resolve().parent.parent / "demo" / "samples"


def obs(value, scenario=None, as_of=None, conf=0.9, metric="arr"):
    return Observation(metric, value, "slide_1", value, "regex", conf, as_of_date=as_of, scenario=scenario)


class SampleDeckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {p.stem: analyse(p.stem, p.read_bytes()) for p in SAMPLES.glob("*.pdf")}

    def test_ranking_order(self):
        order = [d.name for d in rank(list(self.results.values()))]
        self.assertEqual(order, ["fintech_a_series_a", "healthtech_b_seed", "climate_c_pre_seed"])

    def test_every_value_is_quoted(self):
        for d in self.results.values():
            for o in d.values:
                self.assertTrue(o.evidence_text.strip(), o.metric_name)

    def test_revenue_not_read_as_year(self):
        rev = [o for o in self.results["fintech_a_series_a"].values if o.metric_name == "revenue"]
        self.assertEqual(rev[0].value, "1800000")

    def test_deck_cannot_complete_must_haves(self):
        self.assertIn("Unit Economics", self.results["fintech_a_series_a"].missing_must_have_types)


class PickWinnerTests(unittest.TestCase):
    def test_actual_beats_projection_and_latest_wins(self):
        winners = pick_winners([
            obs("1", "projection", "2027-12-31"),
            obs("2", "actual", "2025-12-31"),
            obs("3", "actual", "2026-04-30"),
        ])
        self.assertEqual(winners[0].value, "3")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.budget = DailyBudget(limit=2, path=Path(tempfile.mkdtemp()) / "b.json")

    def test_budget_stops_calls(self):
        class Echo:
            def complete(self, prompt, **kw):
                return "{}"
        p = BudgetedProvider(Echo(), self.budget)
        p.complete("a")
        p.complete("b")
        with self.assertRaises(BudgetExhausted):
            p.complete("c")
        self.assertEqual(self.budget.remaining(), 0)

    def test_rejects_non_pdf_and_large_files(self):
        self.assertIsNotNone(check_upload(b"hello"))
        self.assertIsNotNone(check_upload(b"%PDF" + b"0" * (11 * 1024 * 1024)))
        self.assertIsNone(check_upload((SAMPLES / "fintech_a_series_a.pdf").read_bytes()))


if __name__ == "__main__":
    unittest.main()
