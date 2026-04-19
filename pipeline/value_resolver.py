"""
value_resolver — picks the winning metric_observation per (company, metric)
and stores it on company_metric_value.

Why this module exists:
  In the Phase 1 architecture (see ARCHITECTURE.md §5.3), observations are
  immutable and append-only. Many sources can disagree about the same
  (company, metric) pair. This module decides which observation wins and
  caches the result on company_metric_value for fast reads by the UI.

Resolver rules (in order):
  1. If an analyst has set override=TRUE on the company_metric_value row,
     that row wins unconditionally. No observation may overwrite it.
     (See ARCHITECTURE.md §8.)
  2. Else the winning observation is picked by:
       a. Lowest source priority rank (analyst_override=1, seed=7, …)
       b. Most recent captured_at (newer wins within same priority)
       c. Highest confidence (tie-breaker)
  3. The canonical company_metric_value row is upserted with the winner's
     value + a pointer to winning_observation_id.

Public API:
  resolve_and_store(company_id, metric_id) -> dict | None
  resolve_all_for_company(company_id) -> int
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source priority ladder (ARCHITECTURE.md §8)
#
# Lower number = higher priority (wins ties). The extractor's `name` is the
# lookup key so future extractors slot into this table without schema changes.
#
# When an extractor is missing from this dict, it gets the DEFAULT_PRIORITY
# (lower than known sources but higher than seed data) — safer to trust an
# unknown but recent signal than to trust decade-old seed data.
# ---------------------------------------------------------------------------
SOURCE_PRIORITY: Dict[str, int] = {
    "analyst_override":         1,
    "founder_interview":        2,   # future: extractors/founder_interview_v1
    "meeting_note_md_v1":       2,   # Phase 2
    "pitchdeck_v1":             3,   # Phase 2a
    "crunchbase":               4,   # future
    "sec_filing":               4,   # future
    "linkedin":                 5,   # future
    "alpha_scout_v1":           5,
    "news_article":             6,   # future
    "seed_data_v1":             7,   # demo baseline
}

DEFAULT_PRIORITY = 6


def _priority_for(extractor_name: Optional[str]) -> int:
    """Return the source priority rank for an extractor name.
    Unknown extractors get DEFAULT_PRIORITY so they're neither best nor worst."""
    if not extractor_name:
        return DEFAULT_PRIORITY
    return SOURCE_PRIORITY.get(extractor_name, DEFAULT_PRIORITY)


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------

def resolve_and_store(company_id: str, metric_id: str) -> Optional[Dict]:
    """
    Resolve the winning observation for (company_id, metric_id) and
    persist it on company_metric_value.

    Returns the updated/created company_metric_value row as a dict, or None
    if no observations exist AND no override exists (nothing to show).
    """

    # Step 1 — check for an analyst override. If one exists, that row is
    # sacred. We do NOT touch it.
    existing_cmv = db._fetchone("""
        SELECT *
        FROM company_metric_value
        WHERE company_id = %s AND metric_id = %s AND is_latest = TRUE
    """, (company_id, metric_id))

    if existing_cmv and existing_cmv.get("override") is True:
        logger.debug(
            f"  🔒 {company_id[:8]}/{metric_id[:8]} locked by override; skip"
        )
        return existing_cmv

    # Step 2 — load all candidate observations for this (company, metric).
    # Join to extractor so we can apply the priority ladder.
    candidates = db._fetchall("""
        SELECT
            o.id                      AS observation_id,
            o.normalized_value,
            o.raw_value,
            o.evidence_text,
            o.evidence_url,
            o.confidence,
            o.captured_at,
            o.source_document_id,
            e.name                    AS extractor_name
        FROM metric_observation o
        JOIN extraction_run r ON r.id = o.extraction_run_id
        JOIN extractor      e ON e.id = r.extractor_id
        WHERE o.company_id = %s AND o.metric_id = %s
    """, (company_id, metric_id))

    if not candidates:
        logger.debug(
            f"  ∅ {company_id[:8]}/{metric_id[:8]} no observations yet"
        )
        return existing_cmv  # may be None

    # Step 3 — sort by (priority ASC, captured_at DESC, confidence DESC).
    # Python's sorted() is stable, so equal priorities fall through to the
    # next key.
    def sort_key(row):
        return (
            _priority_for(row["extractor_name"]),
            # negate times & confidences so DESC sorts via ASC of negation
            -(row["captured_at"].timestamp() if row["captured_at"] else 0),
            -(float(row["confidence"] or 0)),
        )

    winner = sorted(candidates, key=sort_key)[0]

    logger.info(
        f"  🏆 {company_id[:8]}/{metric_id[:8]} winner="
        f"{winner['extractor_name']} "
        f"(rank={_priority_for(winner['extractor_name'])}, "
        f"value={winner['normalized_value']})"
    )

    # Step 4 — demote any previous is_latest=TRUE row so the UNIQUE index
    # (company_id, metric_id) WHERE is_latest = TRUE holds.
    db._execute("""
        UPDATE company_metric_value
        SET is_latest = FALSE
        WHERE company_id = %s AND metric_id = %s AND is_latest = TRUE
    """, (company_id, metric_id))

    # Step 5 — insert the new canonical row. We reference the winning
    # observation so the UI can drill down to source_document → chunk.
    new_row = db._execute("""
        INSERT INTO company_metric_value (
            company_id, metric_id, value,
            raw_evidence, evidence_url, captured_at,
            captured_by, confidence, override, is_latest,
            winning_observation_id
        )
        VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, FALSE, TRUE,
            %s
        )
        RETURNING *
    """, (
        company_id, metric_id, winner["normalized_value"],
        winner["evidence_text"], winner["evidence_url"], winner["captured_at"],
        winner["extractor_name"], float(winner["confidence"] or 1.0),
        winner["observation_id"],
    ))

    return new_row


def resolve_all_for_company(company_id: str) -> int:
    """
    Re-run the resolver for every metric this company has observations for.
    Useful after a bulk ingest.

    Returns the number of (company, metric) pairs processed.
    """
    metric_ids = db._fetchall("""
        SELECT DISTINCT metric_id FROM metric_observation WHERE company_id = %s
    """, (company_id,))

    count = 0
    for row in metric_ids:
        resolve_and_store(company_id, str(row["metric_id"]))
        count += 1
    logger.info(f"Resolved {count} metric(s) for company {company_id[:8]}")
    return count


# ---------------------------------------------------------------------------
# CLI — for ad-hoc runs after a manual DB tweak
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Re-resolve winning metric values from observations."
    )
    parser.add_argument("--company-id", help="Resolve all metrics for this company.")
    parser.add_argument(
        "--metric-id",
        help="Resolve a single (company, metric) pair. Requires --company-id.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Resolve every (company, metric) pair in the DB.",
    )
    args = parser.parse_args()

    if args.metric_id:
        if not args.company_id:
            sys.exit("--metric-id requires --company-id")
        resolve_and_store(args.company_id, args.metric_id)
    elif args.company_id:
        resolve_all_for_company(args.company_id)
    elif args.all:
        companies = db._fetchall("SELECT DISTINCT company_id FROM metric_observation")
        total = 0
        for c in companies:
            total += resolve_all_for_company(str(c["company_id"]))
        print(f"✅ Resolved {total} pairs across {len(companies)} companies")
    else:
        parser.error("Provide one of --company-id, --metric-id, --all")
