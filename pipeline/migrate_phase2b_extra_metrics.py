"""Phase 2b migration: widen the pitch-deck metric coverage.

Two responsibilities, both idempotent:

  1.  Backfill `code` on TWO existing metric rows that we now want to extract
      from decks (Technical Co-Founder, Runway months). No new data — just a
      stable handle for rules.yaml.

  2.  Insert FIVE new metric rows under the Financials metric_type for the
      numbers the user's deck actually talks about (ARR, Revenue, Gross
      Margin, Burn, Customers). These rows are born with a `code` already
      set, so rules.yaml can reference them immediately.

Why a dedicated migration (not seed.sql):
  - seed.sql is the bootstrap for fresh DBs. Running it against an existing
    DB would re-run demo INSERTs with ON CONFLICT DO NOTHING — fine, but it
    wouldn't backfill `code` on rows that already exist. Migrations are the
    right tool for "change live data in place".
  - Everything here uses deterministic UUIDs so re-runs are no-ops.

Usage:  python3 -m pipeline.migrate_phase2b_extra_metrics
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402

# ---------------------------------------------------------------------------
# Part 1 — backfill code on existing rows (match by id, NOT by name).
# Matching by id is safe against UI renames.
# ---------------------------------------------------------------------------
BACKFILL_EXISTING = [
    # ('metric.id', 'new code')
    ("33333333-0000-0000-0000-000000000003", "technical_cofounder"),
    ("33333333-0000-0000-0000-000000000016", "runway_months"),
]

# ---------------------------------------------------------------------------
# Part 2 — insert new Financials metrics. All deterministic UUIDs so repeated
# runs don't duplicate. metric_type_id points at Financials (22222222-...006).
#
# Tuple shape matches the INSERT below:
#   (id, code, name, description, value_type, must_have, obtain_method, weight)
# ---------------------------------------------------------------------------
FINANCIALS_TYPE_ID = "22222222-0000-0000-0000-000000000006"

NEW_METRICS = [
    (
        "33333333-0000-0000-0000-000000000021",
        "arr",
        "Annual Recurring Revenue",
        "ARR in USD (latest actual, not projection)",
        "number", False, "Pitch Deck", 1.0,
    ),
    (
        "33333333-0000-0000-0000-000000000022",
        "revenue",
        "Revenue",
        "Annual revenue in USD (latest actual)",
        "number", False, "Pitch Deck", 0.8,
    ),
    (
        "33333333-0000-0000-0000-000000000023",
        "gross_margin",
        "Gross Margin",
        "Gross margin as a percentage (0-100)",
        "number", False, "Pitch Deck", 0.8,
    ),
    (
        "33333333-0000-0000-0000-000000000024",
        "burn_rate",
        "Burn Rate",
        "Monthly cash burn in USD (absolute value, positive)",
        "number", False, "Pitch Deck", 0.6,
    ),
    (
        "33333333-0000-0000-0000-000000000025",
        "customer_count",
        "Customer Count",
        "Number of paying customers (latest actual)",
        "number", False, "Pitch Deck", 0.5,
    ),
]


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        # --- Part 1: backfill codes -------------------------------------
        # IS DISTINCT FROM keeps re-runs silent (no UPDATE when nothing changes).
        print("[migrate] backfilling code on existing metrics")
        for mid, code in BACKFILL_EXISTING:
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

        # --- Part 2: insert new Financials metrics ----------------------
        # ON CONFLICT (id) DO NOTHING — first run inserts, subsequent runs no-op.
        # We pass metric_type_id separately so each tuple stays short.
        print("[migrate] inserting new Financials metrics")
        for (mid, code, name, desc, vt, must, obtain, weight) in NEW_METRICS:
            cur.execute(
                """
                INSERT INTO metric
                    (id, metric_type_id, name, description,
                     value_type, must_have, obtain_method, weight, code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (mid, FINANCIALS_TYPE_ID, name, desc,
                 vt, must, obtain, weight, code),
            )
            print(f"  {code:25s} ({mid}) rows_inserted={cur.rowcount}")

        # --- Verify -----------------------------------------------------
        # Show every coded metric so you can eyeball the full registry.
        cur.execute(
            "SELECT code, name, value_type FROM metric "
            "WHERE code IS NOT NULL ORDER BY code"
        )
        rows = cur.fetchall()
        print(f"[verify] total metrics with a code: {len(rows)}")
        for r in rows:
            print(f"  {r['code']:25s} {r['value_type']:10s} {r['name']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
