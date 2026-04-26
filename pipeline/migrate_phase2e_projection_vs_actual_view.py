"""Phase 2e migration: projection-vs-actual comparison view.

Why:
  Time-series persistence lets us keep both forward-looking scenarios
  (estimate/projection/forecast) and later actuals. This view helps analysts
  validate company forecast quality by comparing projected values against
  realized actuals for the same company + metric + as_of_date.

Usage:
  python3 -m pipeline.migrate_phase2e_projection_vs_actual_view
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402


MIGRATION_SQL = """
CREATE OR REPLACE VIEW metric_projection_vs_actual AS
WITH projected_ranked AS (
    SELECT
        mo.*,
        ROW_NUMBER() OVER (
            PARTITION BY mo.company_id, mo.metric_id, mo.as_of_date
            ORDER BY mo.captured_at DESC, mo.created_at DESC, mo.id DESC
        ) AS rn
    FROM metric_observation mo
    WHERE mo.scenario IN ('estimate', 'projection', 'forecast')
      AND mo.as_of_date IS NOT NULL
),
actual_ranked AS (
    SELECT
        mo.*,
        ROW_NUMBER() OVER (
            PARTITION BY mo.company_id, mo.metric_id, mo.as_of_date
            ORDER BY mo.captured_at DESC, mo.created_at DESC, mo.id DESC
        ) AS rn
    FROM metric_observation mo
    WHERE mo.scenario = 'actual'
      AND mo.as_of_date IS NOT NULL
),
projected_latest AS (
    SELECT * FROM projected_ranked WHERE rn = 1
),
actual_latest AS (
    SELECT * FROM actual_ranked WHERE rn = 1
)
SELECT
    p.company_id,
    c.name AS company_name,
    p.metric_id,
    m.code AS metric_code,
    p.as_of_date AS target_date,
    p.scenario AS projected_scenario,
    p.normalized_value AS projected_value,
    p.period_label AS projected_period_label,
    p.captured_at AS projected_captured_at,
    p.extraction_run_id AS projected_extraction_run_id,
    a.normalized_value AS actual_value,
    a.period_label AS actual_period_label,
    a.captured_at AS actual_captured_at,
    a.extraction_run_id AS actual_extraction_run_id,
    CASE
        WHEN p.normalized_value ~ '^-?[0-9]+(\\.[0-9]+)?$'
         AND a.normalized_value ~ '^-?[0-9]+(\\.[0-9]+)?$'
        THEN a.normalized_value::NUMERIC - p.normalized_value::NUMERIC
        ELSE NULL
    END AS delta_numeric,
    CASE
        WHEN p.normalized_value ~ '^-?[0-9]+(\\.[0-9]+)?$'
         AND a.normalized_value ~ '^-?[0-9]+(\\.[0-9]+)?$'
         AND ABS(p.normalized_value::NUMERIC) > 0
        THEN (a.normalized_value::NUMERIC - p.normalized_value::NUMERIC)
             / ABS(p.normalized_value::NUMERIC)
        ELSE NULL
    END AS delta_pct
FROM projected_latest p
JOIN actual_latest a
  ON a.company_id = p.company_id
 AND a.metric_id = p.metric_id
 AND a.as_of_date = p.as_of_date
JOIN company c ON c.id = p.company_id
JOIN metric m ON m.id = p.metric_id;
"""


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        print("[migrate] creating metric_projection_vs_actual view")
        cur.execute(MIGRATION_SQL)

        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM information_schema.views
             WHERE table_schema = 'public'
               AND table_name = 'metric_projection_vs_actual'
            """
        )
        present = int(cur.fetchone()["cnt"])
        print(f"[verify] view present: {present == 1}")

        cur.execute("SELECT COUNT(*) AS rows FROM metric_projection_vs_actual")
        rows = int(cur.fetchone()["rows"])
        print(f"[verify] current matched projection-vs-actual rows: {rows}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
