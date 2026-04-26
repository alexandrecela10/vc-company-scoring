"""Phase 2d migration: time-series-safe metric observations.

Why:
  We now extract multi-period facts from decks (e.g. ARR 2024, 2025, 2026E,
  2027P, 2028P). The old uniqueness rule
  UNIQUE(extraction_run_id, company_id, metric_id)
  keeps only one row per metric per run, collapsing period-level detail.

What this migration does:
  1) Adds `observation_fingerprint` (idempotency key).
  2) Backfills fingerprints for existing rows.
  3) Drops legacy uniqueness constraint.
  4) Creates UNIQUE index on observation_fingerprint.

Design choice:
  Fingerprint is content-based, not run-id-based, so re-running extraction on
  the same source facts is a no-op, while genuinely new facts still append.

Usage:
  python3 -m pipeline.migrate_phase2d_timeseries_persistence
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402


MIGRATION_SQL = """
-- 1) Add fingerprint column --------------------------------------------------
ALTER TABLE metric_observation
    ADD COLUMN IF NOT EXISTS observation_fingerprint TEXT;

-- 2) Backfill + disambiguate existing rows ----------------------------------
-- Some legacy datasets may already contain exact duplicate grounded facts
-- across runs. We keep all rows (no data loss): first copy keeps base hash,
-- duplicates get "<base>:<id>" so unique index can be created safely.
WITH base AS (
    SELECT
        id,
        md5(concat_ws(
            '||',
            company_id::text,
            metric_id::text,
            COALESCE(normalized_value, ''),
            COALESCE(as_of_date::text, ''),
            COALESCE(scenario, ''),
            COALESCE(period_label, ''),
            COALESCE(currency, ''),
            COALESCE(source_document_id::text, ''),
            COALESCE(source_chunk_id::text, ''),
            COALESCE(evidence_text, '')
        )) AS base_fp
    FROM metric_observation
), ranked AS (
    SELECT
        id,
        base_fp,
        row_number() OVER (PARTITION BY base_fp ORDER BY id) AS rn
    FROM base
)
UPDATE metric_observation mo
   SET observation_fingerprint = CASE
       WHEN r.rn = 1 THEN r.base_fp
       ELSE r.base_fp || ':' || mo.id::text
   END
  FROM ranked r
 WHERE mo.id = r.id;

-- 3) Enforce NOT NULL after backfill ----------------------------------------
ALTER TABLE metric_observation
    ALTER COLUMN observation_fingerprint SET NOT NULL;

-- 4) Remove legacy one-row-per-metric-per-run constraint --------------------
ALTER TABLE metric_observation
    DROP CONSTRAINT IF EXISTS metric_observation_extraction_run_id_company_id_metric_id_key;

-- 5) Add new uniqueness on fingerprint --------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_metric_observation_fingerprint
    ON metric_observation (observation_fingerprint);

-- 6) Helpful index for time-series timeline queries -------------------------
CREATE INDEX IF NOT EXISTS idx_metric_observation_timeline
    ON metric_observation (company_id, metric_id, as_of_date DESC, scenario, captured_at DESC);

COMMENT ON COLUMN metric_observation.observation_fingerprint IS
    'Stable idempotency key for a grounded observation (metric + value + temporal + provenance fields).';
"""


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        print("[migrate] applying phase2d time-series persistence migration")
        cur.execute(MIGRATION_SQL)

        cur.execute(
            """
            SELECT is_nullable
              FROM information_schema.columns
             WHERE table_name='metric_observation'
               AND column_name='observation_fingerprint'
            """
        )
        col = cur.fetchone()
        print(
            f"[verify] observation_fingerprint nullable={col['is_nullable'] if col else 'MISSING'}"
        )

        cur.execute(
            """
            SELECT COUNT(*) AS missing
              FROM metric_observation
             WHERE observation_fingerprint IS NULL
            """
        )
        missing = int(cur.fetchone()["missing"])
        print(f"[verify] rows missing fingerprint: {missing}")

        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'metric_observation'
               AND indexname = 'uq_metric_observation_fingerprint'
            """
        )
        idx_cnt = int(cur.fetchone()["cnt"])
        print(f"[verify] unique fingerprint index present: {idx_cnt == 1}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
