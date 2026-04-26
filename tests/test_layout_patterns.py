"""Tests for reusable pitch-deck layout pattern extraction."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from pipeline.extractors.layout_patterns import extract_layout_observations, infer_deck_date
from pipeline.preprocessors.base import Chunk

RULES_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "rules.yaml"


class LayoutPatternTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = yaml.safe_load(RULES_PATH.read_text())

    def test_infers_deck_date_from_front_slide(self):
        chunks = [Chunk("SERIES A April 2026 Raising $14M", "slide_1", 0)]
        self.assertEqual(infer_deck_date(chunks), "2026-04-30")

    def test_extracts_kpi_cards_using_deck_date(self):
        chunks = [
            Chunk("SERIES A April 2026", "slide_1", 0),
            Chunk(
                "TRACTION From 0 to $4.2M ARR in 19 months. "
                "$4.2M 62 138% $380K ARR Paying customers Net revenue retention Avg contract value",
                "slide_5",
                4,
            ),
        ]
        obs = extract_layout_observations(chunks, self.rules)
        by_metric = {o.metric_name: o for o in obs}

        self.assertEqual(by_metric["arr"].value, "4200000")
        self.assertEqual(by_metric["arr"].as_of_date, "2026-04-30")
        self.assertEqual(by_metric["arr"].scenario, "actual")
        self.assertEqual(by_metric["arr"].method, "layout_pattern")
        self.assertEqual(by_metric["customer_count"].value, "62")
        self.assertEqual(by_metric["customer_count"].as_of_date, "2026-04-30")

    def test_extracts_arr_milestone_as_projection(self):
        chunks = [
            Chunk("SERIES A April 2026", "slide_1", 0),
            Chunk("MILESTONES Reach $22M ARR by end of 2027", "slide_11", 10),
        ]
        obs = extract_layout_observations(chunks, self.rules)
        arr = [o for o in obs if o.metric_name == "arr"][0]

        self.assertEqual(arr.value, "22000000")
        self.assertEqual(arr.as_of_date, "2027-12-31")
        self.assertEqual(arr.scenario, "projection")
        self.assertEqual(arr.period_label, "2027")

    def test_extracts_team_growth_milestone_as_projection(self):
        chunks = [
            Chunk("SERIES A April 2026", "slide_1", 0),
            Chunk("MILESTONES Reach $22M ARR by end of 2027 Grow team from 28 to 95", "slide_11", 10),
        ]
        obs = extract_layout_observations(chunks, self.rules)
        employee = [o for o in obs if o.metric_name == "employee_count"][0]

        self.assertEqual(employee.value, "95")
        self.assertEqual(employee.as_of_date, "2027-12-31")
        self.assertEqual(employee.scenario, "projection")

    def test_extracts_runway_with_deck_date_fallback(self):
        chunks = [
            Chunk("SERIES A April 2026", "slide_1", 0),
            Chunk("THE ASK Raising $14M Series A. 24-month runway", "slide_11", 10),
        ]
        obs = extract_layout_observations(chunks, self.rules)
        runway = [o for o in obs if o.metric_name == "runway_months"][0]

        self.assertEqual(runway.value, "24")
        self.assertEqual(runway.as_of_date, "2026-04-30")
        self.assertEqual(runway.scenario, "actual")

    def test_extracts_flattened_financial_matrix(self):
        chunks = [
            Chunk(
                "FINANCIALS Path to $52M ARR by 2028. 2024 2025 2026E 2027P 2028P "
                "ARR ($M) 0.5 2.4 7.8 22.0 52.0 "
                "Revenue ($M) 0.3 1.8 6.2 18.5 44.0 "
                "Gross margin 64% 71% 76% 79% 81% "
                "Burn ($M) (1.8) (4.2) (6.5) (3.1) 8.5 "
                "Customers 9 38 95 210 410 "
                "Headcount 12 28 52 95 155",
                "slide_10",
                9,
            )
        ]
        obs = extract_layout_observations(chunks, self.rules)
        arr = [o for o in obs if o.metric_name == "arr" and o.period_label == "2025"][0]
        revenue = [o for o in obs if o.metric_name == "revenue" and o.period_label == "2025"][0]
        gross_margin = [o for o in obs if o.metric_name == "gross_margin" and o.period_label == "2025"][0]
        burn = [o for o in obs if o.metric_name == "burn_rate" and o.period_label == "2025"][0]
        customers = [o for o in obs if o.metric_name == "customer_count" and o.period_label == "2025"][0]
        headcount = [o for o in obs if o.metric_name == "employee_count" and o.period_label == "2025"][0]

        self.assertEqual(arr.value, "2400000")
        self.assertEqual(revenue.value, "1800000")
        self.assertEqual(gross_margin.value, "71")
        self.assertEqual(burn.value, "4200000")
        self.assertEqual(customers.value, "38")
        self.assertEqual(headcount.value, "28")
        self.assertEqual(arr.scenario, "actual")
        self.assertEqual([o.scenario for o in obs if o.period_label == "2027P"][0], "projection")


if __name__ == "__main__":
    unittest.main()
