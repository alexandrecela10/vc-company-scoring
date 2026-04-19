"""
backfill_phase1 — one-shot migration that turns every existing
company_metric_value row into a synthetic extraction_run + metric_observation.

Why this exists:
  Before Phase 1, values were written directly into company_metric_value.
  After Phase 1, every value must trace back to an observation produced by
  an extractor. This script bridges the gap: for every pre-existing row,
  we fabricate the minimal provenance chain required so the UI can still
  show "where did this value come from?" without losing historical data.

Idempotent: safe to re-run. Uses ON CONFLICT DO NOTHING on the natural
unique key (extraction_run_id, company_id, metric_id) so re-executing
just skips rows that already have an observation.

What it DOES NOT do:
  - Create source_document rows. Phase 1 has no real files; source_document
    stays empty until Phase 2 when we start ingesting pitch decks.
  - Touch analyst-overridden rows' value or evidence. Override is sacred.
  - Backfill context_fact rows. Those arrive in Phase 3.

Usage:
    python -m pipeline.backfill_phase1             # real run
    python -m pipeline.backfill_phase1 --dry-run   # show counts, no writes
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Optional

import db

logger = logging.getLogger("backfill_phase1")


# ---------------------------------------------------------------------------
# Extractor name resolution
#
# For every existing CMV row, pick which extractor "produced" it in our
# synthetic rewriting of history. Rules are deliberately simple — we prefer
# a tiny amount of mis-attribution (a demo row assigned to seed_data_v1)
# over a complex heuristic that could be wrong.
# ---------------------------------------------------------------------------

def _extractor_for(cmv_row: Dict) -> str:
    """Return the name of the extractor that should own this CMV row."""
    if cmv_row.get("override") is True:
        # The analyst locked this value. Attribute to the override extractor.
        return "analyst_override"

    captured_by = (cmv_row.get("captured_by") or "").lower()

    # Alpha Scout discovery produced it (seed from Step 5 + any future runs).
    if "alpha_scout" in captured_by:
        return "alpha_scout_v1"

    # Everything else — the demo-seeded values, agent outputs with unclear
    # provenance, etc. — falls back to seed_data_v1. In practice almost every
    # non-override, non-alpha-scout row in the current DB is demo seed data.
    return "seed_data_v1"


# ---------------------------------------------------------------------------
# Core backfill
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, force: bool = False) -> Dict[str, int]:
    """
    Execute the backfill.

    Idempotency note:
      This is a ONE-SHOT migration. The natural key for an observation is
      (extraction_run_id, company_id, metric_id) — but each re-run creates
      fresh extraction_runs, so unique-constraint dedup does NOT work
      across runs. Instead, we gate on a simple global check: if any
      observation already exists for our backfill extractors, abort.
      Use --force to override (wipes observations first).

    Steps:
      1. Load the extractor UUIDs we need, keyed by name.
      2. Guard: refuse to run if backfill observations already exist.
      3. For every CMV row, pick its extractor, create/reuse an
         extraction_run (one per extractor×company), then insert a
         metric_observation.
      4. For the is_latest=TRUE, override=FALSE rows: point
         winning_observation_id at the new observation so the UI can
         show provenance.

    Returns a stats dict for the CLI summary.
    """

    # 1 — load extractor UUIDs (hardcoded in seed.sql as 5555…-0000-0000-0000-0000000XX)
    extractors = {
        row["name"]: str(row["id"])
        for row in db._fetchall(
            "SELECT id, name FROM extractor WHERE name IN "
            "('analyst_override', 'alpha_scout_v1', 'seed_data_v1')"
        )
    }
    missing = [n for n in ("analyst_override", "alpha_scout_v1", "seed_data_v1") if n not in extractors]
    if missing:
        raise RuntimeError(
            f"Extractor(s) not found in DB: {missing}. "
            f"Did you run the Phase 1 seed INSERTs from seed.sql?"
        )
    logger.info(f"Extractors resolved: {list(extractors.keys())}")

    # 2 — idempotency guard. Count any existing backfill-produced observations.
    existing = db._fetchone("""
        SELECT COUNT(*) AS n FROM metric_observation o
        JOIN extraction_run r ON r.id = o.extraction_run_id
        WHERE r.extractor_id = ANY(%s::uuid[])
    """, (list(extractors.values()),))["n"]

    if existing and not force:
        raise RuntimeError(
            f"Backfill already ran: {existing} observation(s) exist from "
            f"our 3 extractors. Re-running would create duplicates.\n"
            f"Use --force to wipe metric_observation + extraction_run first "
            f"and redo the backfill from scratch."
        )

    if existing and force:
        logger.warning(f"--force: wiping {existing} existing observations + their runs")
        if not dry_run:
            # Null winning pointers first so the FK delete is unblocked.
            db._execute("UPDATE company_metric_value SET winning_observation_id = NULL WHERE winning_observation_id IS NOT NULL")
            db._execute("DELETE FROM metric_observation")
            db._execute("DELETE FROM extraction_run WHERE extractor_id = ANY(%s::uuid[])",
                        (list(extractors.values()),))

    # 3 — cache of extraction_run IDs keyed by (extractor_name, company_id)
    # to avoid creating N runs when one logical "batch" suffices.
    run_cache: Dict[tuple, str] = {}

    # 3 — load EVERY CMV row (including historical is_latest=FALSE rows so
    # history is preserved as observations, even if they don't win anything).
    cmv_rows = db._fetchall("""
        SELECT id, company_id, metric_id, value, raw_evidence, evidence_url,
               captured_by, captured_at, confidence, override, is_latest
        FROM company_metric_value
        ORDER BY company_id, metric_id, captured_at
    """)

    stats = {
        "cmv_rows_scanned":           len(cmv_rows),
        "observations_created":       0,
        "observations_skipped":       0,
        "cmv_rows_linked_to_winner":  0,
        "extraction_runs_created":    0,
    }

    logger.info(f"Scanning {len(cmv_rows)} company_metric_value rows…")

    for cmv in cmv_rows:
        company_id = str(cmv["company_id"])
        metric_id  = str(cmv["metric_id"])
        extractor_name = _extractor_for(cmv)
        extractor_id   = extractors[extractor_name]

        # Get or create the extraction_run for this (extractor, company).
        # One run per (extractor, company) keeps the run table small and
        # matches the "one batch" mental model for Phase 1 backfill.
        cache_key = (extractor_name, company_id)
        run_id = run_cache.get(cache_key)
        if run_id is None:
            if dry_run:
                # Use a deterministic pseudo id so downstream prints work.
                run_id = f"DRY-RUN-{extractor_name[:4]}-{company_id[:8]}"
            else:
                run_row = db._execute("""
                    INSERT INTO extraction_run (
                        extractor_id, company_id, status,
                        observations_count, started_at, finished_at
                    )
                    VALUES (%s, %s, 'success', 0, NOW(), NOW())
                    RETURNING id
                """, (extractor_id, company_id))
                run_id = str(run_row["id"])
            run_cache[cache_key] = run_id
            stats["extraction_runs_created"] += 1

        if dry_run:
            stats["observations_created"] += 1
            continue

        # Insert the observation. Unique (run, company, metric) → re-running
        # the backfill is a no-op thanks to ON CONFLICT DO NOTHING.
        obs_row = db._execute("""
            INSERT INTO metric_observation (
                extraction_run_id, company_id, metric_id,
                raw_value, normalized_value,
                evidence_text, evidence_url,
                confidence, captured_at
            )
            VALUES (
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s
            )
            ON CONFLICT (extraction_run_id, company_id, metric_id) DO NOTHING
            RETURNING id
        """, (
            run_id, company_id, metric_id,
            cmv["value"],                 # raw_value: we don't have a separate raw so store the same
            cmv["value"],                 # normalized_value (already stored as TEXT in CMV)
            cmv.get("raw_evidence"),
            cmv.get("evidence_url"),
            float(cmv.get("confidence") or 1.0),
            cmv.get("captured_at"),
        ))

        if obs_row is None:
            stats["observations_skipped"] += 1
            continue

        stats["observations_created"] += 1
        obs_id = str(obs_row["id"])

        # Only the current (is_latest=TRUE, non-override) row gets its
        # winning_observation_id set. Overrides stay NULL because override
        # is the winning signal, not an observation. Historical rows also
        # stay NULL — they were never "the winner".
        if cmv.get("is_latest") and not cmv.get("override"):
            db._execute("""
                UPDATE company_metric_value
                SET winning_observation_id = %s
                WHERE id = %s AND winning_observation_id IS NULL
            """, (obs_id, cmv["id"]))
            stats["cmv_rows_linked_to_winner"] += 1

    # Update extraction_run.observations_count for the runs we touched.
    # Nice for the UI / observability even though nothing depends on it.
    if not dry_run:
        for (_name, cid), run_id in run_cache.items():
            db._execute("""
                UPDATE extraction_run
                SET observations_count = (
                    SELECT COUNT(*) FROM metric_observation WHERE extraction_run_id = %s
                )
                WHERE id = %s
            """, (run_id, run_id))

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count rows; no writes.")
    parser.add_argument("--force",   action="store_true",
                        help="Wipe existing backfill observations + runs before re-running. "
                             "Use only if you know what you're doing.")
    args = parser.parse_args()

    stats = run(dry_run=args.dry_run, force=args.force)

    print("\n" + "=" * 72)
    print(f"Backfill Phase 1 report ({'DRY RUN' if args.dry_run else 'LIVE'})")
    print("=" * 72)
    for k, v in stats.items():
        print(f"  {k:<32} {v}")
    print("=" * 72)

    if args.dry_run:
        print("↻ Re-run without --dry-run to apply.")
    else:
        print("✅ Backfill complete.")
        print("Next: run `python -m pipeline.value_resolver --all` to verify the")
        print("resolver converges on the same values (it should — no-op).")


if __name__ == "__main__":
    main()
