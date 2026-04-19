"""Phase 2a migration runner.

Adds `lifecycle_state` + `deactivated_at` columns to `metric` and rebuilds
`company_score_view` to exclude draft/archived metrics.

Idempotent: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE VIEW means safe to
re-run. Verification queries at the end confirm the migration landed cleanly.

Usage:  python -m pipeline.migrate_phase2a
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sibling modules importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402

# The migration lives in schema.sql so there's a single source of truth.
# We slice out just the Phase 2a block (between the marker comments) and run it.
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
START_MARKER = "-- Phase 2a migration: metric lifecycle states"


def extract_migration_sql() -> str:
    """Return the Phase 2a SQL block from schema.sql (marker -> EOF)."""
    text = SCHEMA_PATH.read_text()
    idx = text.find(START_MARKER)
    if idx < 0:
        raise RuntimeError(f"Could not find Phase 2a marker in {SCHEMA_PATH}")
    return text[idx:]


def main() -> int:
    sql = extract_migration_sql()
    print(f"[migrate] loaded {len(sql)} chars of Phase 2a SQL from schema.sql")

    with get_conn() as conn, conn.cursor() as cur:
        print("[migrate] applying ALTER TABLE + CREATE OR REPLACE VIEW ...")
        cur.execute(sql)
        print("[migrate] ok")

        # --- Verification ---
        # 1) All existing metrics should default to 'active' (18 rows)
        cur.execute(
            "SELECT lifecycle_state, COUNT(*) AS n FROM metric GROUP BY 1 ORDER BY 1"
        )
        states = cur.fetchall()
        print(f"[verify] metric.lifecycle_state breakdown: {states}")

        # 2) Companies still produce score view rows (unchanged count)
        cur.execute("SELECT COUNT(*) AS n FROM company_score_view")
        n_scores = cur.fetchone()["n"]
        print(f"[verify] company_score_view row count: {n_scores}")

        # 3) New columns are visible + default is 'active'
        cur.execute(
            """
            SELECT column_name, data_type, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'metric'
              AND column_name IN ('lifecycle_state', 'deactivated_at')
            ORDER BY column_name
            """
        )
        cols = cur.fetchall()
        print(f"[verify] new metric columns: {cols}")

    print("[migrate] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
