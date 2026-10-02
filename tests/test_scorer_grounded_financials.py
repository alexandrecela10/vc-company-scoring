import unittest

from scorer import _score_metric_type


def _row(metric_id: str, name: str, code: str, value: str, value_type: str = "number"):
    return {
        "metric_id": metric_id,
        "value": value,
        "raw_evidence": f"evidence for {name}",
        "evidence_url": "https://example.com/source",
        "url_verified": True,
        "confidence": 0.9,
        "override": False,
        "override_reason": None,
        "captured_by": "pitchdeck_v1",
        "captured_at": "2026-04-26T10:00:00Z",
        "metric": {
            "name": name,
            "code": code,
            "value_type": value_type,
            "must_have": False,
            "weight": 1.0,
            "obtain_method": "derived",
            "metric_type_id": "mt-fin",
        },
        "data_source": {"name": "Fintech A Deck"},
    }


class GroundedFinancialsScoringTests(unittest.TestCase):
    def test_financials_formula_overrides_generic_numeric_skip(self):
        type_row = {
            "id": "mt-fin",
            "name": "Financials",
            "must_have": True,
            "house_weight": 1.0,
        }
        rows = [
            _row("m-gm", "Gross Margin", "gross_margin", "71"),
            _row("m-runway", "Runway (months)", "runway_months", "24"),
            _row("m-rev", "Revenue", "revenue", "1800000"),
            _row("m-burn", "Burn Rate", "burn_rate", "4200000"),
            _row("m-stage", "Funding Stage", "funding_stage", "3", value_type="score_1_5"),
        ]

        mts = _score_metric_type(type_row, rows)

        self.assertIsNotNone(mts.type_score)
        self.assertAlmostEqual(mts.type_score, 3.16, places=2)
        self.assertTrue(mts.grounded_formula and "FinancialsScore" in mts.grounded_formula)
        self.assertGreaterEqual(len(mts.grounded_formula_inputs), 4)

    def test_financials_formula_works_with_partial_inputs(self):
        type_row = {
            "id": "mt-fin",
            "name": "Financials",
            "must_have": True,
            "house_weight": 1.0,
        }
        rows = [
            _row("m-gm", "Gross Margin", "gross_margin", "64"),
            _row("m-stage", "Funding Stage", "funding_stage", "2", value_type="score_1_5"),
        ]

        mts = _score_metric_type(type_row, rows)

        self.assertIsNotNone(mts.type_score)
        # gross_margin_score = 64/20 = 3.2 (w=0.35), stage=2 (w=0.15)
        # weighted avg = (3.2*0.35 + 2*0.15) / 0.5 = 2.84
        self.assertAlmostEqual(mts.type_score, 2.84, places=2)
        self.assertEqual(len(mts.grounded_formula_inputs), 2)


if __name__ == "__main__":
    unittest.main()


class BurnAnnualisationTests(unittest.TestCase):
    """Bug fix 2026-10-01: annual revenue was divided by monthly burn, so burn efficiency was always 5."""

    def _burn_score(self, burn_granularity):
        rev = _row("m-rev", "Revenue", "revenue", "1800000")
        burn = _row("m-burn", "Burn Rate", "burn_rate", "150000")
        burn["period_granularity"] = burn_granularity
        mts = _score_metric_type({"id": "mt-fin", "name": "Financials"}, [rev, burn])
        return next(i for i in mts.grounded_formula_inputs if i["component"] == "burn_efficiency_score")["derived_score"]

    def test_monthly_burn_is_annualised(self):
        # 1.8M revenue vs 150K x 12 = 1.8M burn -> 1 + 2 x 1.0 = 3.0
        self.assertEqual(self._burn_score("month"), 3.0)

    def test_annual_or_unknown_burn_used_as_is(self):
        # 1.8M / 150K = 12 -> clamped to 5 (unchanged behaviour for rows without granularity)
        self.assertEqual(self._burn_score("year"), 5.0)
        self.assertEqual(self._burn_score(None), 5.0)


class YearColumnGranularityTests(unittest.TestCase):
    def test_year_label_marks_value_annual(self):
        from pipeline.extractors.temporal import granularity_for_label
        self.assertEqual(granularity_for_label("month", "2025"), "year")
        self.assertEqual(granularity_for_label("month", "2026E"), "year")
        self.assertEqual(granularity_for_label("month", "FY2024"), "year")
        self.assertEqual(granularity_for_label("month", "Q3 2025"), "month")
        self.assertEqual(granularity_for_label("month", None), "month")
