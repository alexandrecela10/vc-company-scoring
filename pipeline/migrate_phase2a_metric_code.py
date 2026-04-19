"""Phase 2a migration: add metric.code + backfill the 5 target metrics.

Idempotent: ADD COLUMN IF NOT EXISTS + UPDATE guarded by `IS DISTINCT FROM`
(so re-running is a no-op once codes are in place).

Usage:  python3 -m pipeline.migrate_phase2a_metric_code
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402

MIGRATION_SQL = """
ALTER TABLE metric
    ADD COLUMN IF NOT EXISTS code TEXT UNIQUE
        CHECK (code IS NULL OR code ~ '^[a-z][a-z0-9_]*$');

COMMENT ON COLUMN metric.code IS
    'Stable snake_case identifier used by rules.yaml and non-UI callers. '
    'Immutable by convention. NULL for legacy metrics without extraction rules.';
"""

# Seeded metric IDs -> their new stable codes.
# We match by id (not by name) so a UI rename can't silently point the
# code at the wrong metric.
BACKFILL = [
    ("33333333-0000-0000-0000-000000000014", "mrr"),
    ("33333333-0000-0000-0000-000000000017", "employee_count"),
    ("33333333-0000-0000-0000-000000000015", "funding_stage"),
    ("33333333-0000-0000-0000-000000000018", "founding_year"),
    ("33333333-0000-0000-0000-000000000001", "prior_successful_exit"),
]


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        print("[migrate] metric.code column added / already present")

        # IS DISTINCT FROM handles both NULL -> code and code -> same-code (no-op).
        # Safer than a raw "=" because NULL = anything is NULL in SQL.
        for mid, code in BACKFILL:
            cur.execute(
                """
                UPDATE metric
                   SET code = %s
                 WHERE id = %s
                   AND code IS DISTINCT FROM %s
                """,
                (code, mid, code),
            )
            print(f"  {code:25s} ({mid}) rows_changed={cur.rowcount}")

        # Verify
        cur.execute(
            "SELECT code, name FROM metric WHERE code IS NOT NULL ORDER BY code"
        )
        rows = cur.fetchall()
        print(f"[verify] metrics with a code ({len(rows)}/5 expected):")
        for r in rows:
            print(f"  {r['code']:25s} -> {r['name']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
