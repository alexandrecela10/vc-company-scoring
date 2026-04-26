import unittest

from db import metric_observation_fingerprint


class ObservationFingerprintTests(unittest.TestCase):
    def test_same_payload_yields_same_fingerprint(self):
        row = {
            "metric_id": "m-arr",
            "normalized_value": "2400000",
            "as_of_date": "2025-12-31",
            "scenario": "actual",
            "period_label": "2025",
            "currency": "USD",
            "source_chunk_id": "chunk-1",
            "evidence_text": "ARR ($M) 2.4",
        }
        a = metric_observation_fingerprint("c1", "doc1", row)
        b = metric_observation_fingerprint("c1", "doc1", dict(row))
        self.assertEqual(a, b)

    def test_scenario_change_yields_different_fingerprint(self):
        base = {
            "metric_id": "m-arr",
            "normalized_value": "7800000",
            "as_of_date": "2026-12-31",
            "period_label": "2026E",
            "currency": "USD",
            "source_chunk_id": "chunk-1",
            "evidence_text": "ARR ($M) 7.8",
        }
        actual_fp = metric_observation_fingerprint("c1", "doc1", {**base, "scenario": "actual"})
        estimate_fp = metric_observation_fingerprint("c1", "doc1", {**base, "scenario": "estimate"})
        self.assertNotEqual(actual_fp, estimate_fp)

    def test_date_change_yields_different_fingerprint(self):
        base = {
            "metric_id": "m-revenue",
            "normalized_value": "1800000",
            "scenario": "actual",
            "period_label": "2025",
            "currency": "USD",
            "source_chunk_id": "chunk-2",
            "evidence_text": "Revenue ($M) 1.8",
        }
        fp_2024 = metric_observation_fingerprint("c1", "doc1", {**base, "as_of_date": "2024-12-31"})
        fp_2025 = metric_observation_fingerprint("c1", "doc1", {**base, "as_of_date": "2025-12-31"})
        self.assertNotEqual(fp_2024, fp_2025)


if __name__ == "__main__":
    unittest.main()
