"""Tests for table-aware financial/KPI extraction."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from pipeline.extractors.table_financials import extract_table_observations
from pipeline.preprocessors.base import Chunk

RULES_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "rules.yaml"


def chunk_with_table(table):
    return Chunk(
        text="Financials table extracted by pdfplumber",
        locator="slide_10",
        ordinal=9,
        metadata={"tables": [table], "page_number": 10},
    )


class TableFinancialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = yaml.safe_load(RULES_PATH.read_text())

    def test_financial_table_extracts_each_period_cell(self):
        table = [
            ["Metric", "2024", "2025", "2026E", "2027P", "2028P"],
            ["ARR ($M)", "0.5", "2.4", "7.8", "22", "52"],
        ]
        obs = extract_table_observations([chunk_with_table(table)], self.rules)
        arr = [o for o in obs if o.metric_name == "arr"]

        self.assertEqual(len(arr), 5)
        self.assertEqual([o.value for o in arr], [
            "500000", "2400000", "7800000", "22000000", "52000000",
        ])
        self.assertEqual(arr[0].period_label, "2024")
        self.assertEqual(arr[0].scenario, "actual")
        self.assertEqual(arr[0].as_of_date, "2024-12-31")
        self.assertEqual(arr[2].scenario, "estimate")
        self.assertEqual(arr[2].as_of_date, "2026-12-31")
        self.assertEqual(arr[4].scenario, "projection")
        self.assertEqual(arr[4].as_of_date, "2028-12-31")
        self.assertEqual(arr[4].method, "table")
        self.assertEqual(arr[4].currency, "USD")

    def test_headcount_table_uses_column_year_not_nearby_text(self):
        table = [
            ["KPI", "2024", "2025", "2026E", "2027P", "2028P"],
            ["Headcount", "12", "28", "52", "95", "155"],
        ]
        obs = extract_table_observations([chunk_with_table(table)], self.rules)
        headcount = [o for o in obs if o.metric_name == "employee_count"]

        self.assertEqual(len(headcount), 5)
        self.assertEqual(headcount[0].value, "12")
        self.assertEqual(headcount[0].period_label, "2024")
        self.assertEqual(headcount[0].as_of_date, "2024-12-31")
        self.assertEqual(headcount[0].scenario, "actual")
        self.assertEqual(headcount[3].value, "95")
        self.assertEqual(headcount[3].period_label, "2027P")
        self.assertEqual(headcount[3].scenario, "projection")

    def test_mixed_kpi_table_extracts_percent_and_counts(self):
        table = [
            ["Metric", "2024", "2025"],
            ["Gross Margin", "64%", "71%"],
            ["Customers", "9", "38"],
        ]
        obs = extract_table_observations([chunk_with_table(table)], self.rules)
        by_metric = {o.metric_name: [] for o in obs}
        for o in obs:
            by_metric[o.metric_name].append(o)

        self.assertEqual([o.value for o in by_metric["gross_margin"]], ["64", "71"])
        self.assertEqual([o.value for o in by_metric["customer_count"]], ["9", "38"])
        self.assertEqual(by_metric["gross_margin"][1].scenario, "actual")
        self.assertEqual(by_metric["customer_count"][1].as_of_date, "2025-12-31")

    def test_ignores_non_period_tables(self):
        table = [
            ["Name", "Role"],
            ["Derek", "CEO"],
        ]
        self.assertEqual(extract_table_observations([chunk_with_table(table)], self.rules), [])


if __name__ == "__main__":
    unittest.main()
