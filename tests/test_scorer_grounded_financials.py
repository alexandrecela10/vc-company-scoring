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
        "data_source": {"name": "NovaPay Deck"},
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
