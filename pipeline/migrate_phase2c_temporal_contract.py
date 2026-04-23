"""Phase 2c migration: temporal contract on metric_observation.

Why:
  An extracted "ARR = 2.4M" is ambiguous without knowing WHEN that claim
  applies. Decks often show five years in a row (2024, 2025, 2026E, 2027P,
  2028P) and a naive extraction can't tell whether it picked up a past
  actual, a current-year estimate, or a 2028 projection.

This migration adds FIVE nullable fields to metric_observation. All nullable
so legacy rows stay valid and new extractors can populate fields incrementally.

  as_of_date          DATE      -- the date the CLAIM describes (not when we ran)
  period_granularity  TEXT      -- point_in_time | month | quarter | year | trailing_12m
  scenario            TEXT      -- actual | estimate | projection | forecast
  currency            TEXT      -- ISO-4217 for money metrics (USD, EUR, AED)
  period_label        TEXT      -- raw label from source, e.g. "2026E", for audit

We keep `scenario` NULLABLE (not default='actual') so we can tell
"extractor didn't populate it" (legacy) from "extractor saw an actual"
(new trusted observation). The resolver treats NULL as "unknown".

Idempotent -- ADD COLUMN IF NOT EXISTS + ADD CONSTRAINT guarded by a
catalog lookup.

Usage:  python3 -m pipeline.migrate_phase2c_temporal_contract
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402


MIGRATION_SQL = """
-- 1. Add nullable columns (IF NOT EXISTS keeps this rerunnable) -------------
ALTER TABLE metric_observation
    ADD COLUMN IF NOT EXISTS as_of_date         DATE,
    ADD COLUMN IF NOT EXISTS period_granularity TEXT,
    ADD COLUMN IF NOT EXISTS scenario           TEXT,
    ADD COLUMN IF NOT EXISTS currency           TEXT,
    ADD COLUMN IF NOT EXISTS period_label       TEXT;

-- 2. Enum-like CHECK constraints -------------------------------------------
-- Added only if not already present. NOT VALID would skip row validation
-- but we have no legacy non-NULL rows yet, so a plain CHECK is fine.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'metric_observation_scenario_chk'
    ) THEN
        ALTER TABLE metric_observation
            ADD CONSTRAINT metric_observation_scenario_chk
            CHECK (scenario IS NULL OR scenario IN
                ('actual','estimate','projection','forecast'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'metric_observation_granularity_chk'
    ) THEN
        ALTER TABLE metric_observation
            ADD CONSTRAINT metric_observation_granularity_chk
            CHECK (period_granularity IS NULL OR period_granularity IN
                ('point_in_time','month','quarter','year','trailing_12m'));
    END IF;
END $$;

-- 3. Comments so pgAdmin / Supabase shows intent inline ---------------------
COMMENT ON COLUMN metric_observation.as_of_date IS
    'Date the CLAIM describes (not when we ran). e.g. "ARR 2.4M in 2025" -> 2025-12-31.';
COMMENT ON COLUMN metric_observation.period_granularity IS
    'Shape of the period: point_in_time | month | quarter | year | trailing_12m.';
COMMENT ON COLUMN metric_observation.scenario IS
    'Reality-class of the claim: actual | estimate | projection | forecast. NULL = unknown.';
COMMENT ON COLUMN metric_observation.currency IS
    'ISO-4217 for money metrics. NULL for non-money metrics.';
COMMENT ON COLUMN metric_observation.period_label IS
    'Raw period label from the source, kept verbatim for audit (e.g. "2026E").';

-- 4. Index to accelerate resolver queries (filter actual, order by as_of_date)
-- Partial index: only rows where scenario is set (the common case for the new
-- resolver path). NULL scenarios fall back to the existing lookup paths.
CREATE INDEX IF NOT EXISTS idx_metric_obs_temporal
    ON metric_observation (company_id, metric_id, scenario, as_of_date DESC)
    WHERE scenario IS NOT NULL;
"""


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        print("[migrate] adding temporal contract columns to metric_observation")
        cur.execute(MIGRATION_SQL)

        # Verify -- list the 5 new columns and the 2 constraints.
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'metric_observation'
               AND column_name IN (
                   'as_of_date','period_granularity','scenario',
                   'currency','period_label'
               )
             ORDER BY column_name
            """
        )
        cols = cur.fetchall()
        print(f"[verify] new columns present: {len(cols)}/5")
        for c in cols:
            print(f"  {c['column_name']:20s} {c['data_type']:12s} nullable={c['is_nullable']}")

        cur.execute(
            """
            SELECT conname FROM pg_constraint
             WHERE conname IN (
                'metric_observation_scenario_chk',
                'metric_observation_granularity_chk'
             )
             ORDER BY conname
            """
        )
        checks = cur.fetchall()
        print(f"[verify] CHECK constraints present: {len(checks)}/2")
        for ck in checks:
            print(f"  {ck['conname']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
