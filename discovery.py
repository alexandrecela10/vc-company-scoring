"""
Discovery Pipeline — bridge Alpha Scout → Company Scorer Postgres.

What this does (step by step):
  1. Call Alpha Scout's `search.search_similar_companies` to find startups
     similar to a seed, grounded in trusted sources (Tavily).
  2. For each hit, call Alpha Scout's `source_enrichment.enrich_company` to
     get website + LinkedIn + funding stage in parallel.
  3. Upsert the company into Postgres (dedup by LOWER(name)).
  4. Upsert founders as UNVERIFIED (linkedin resolver runs separately).
  5. Seed whichever scorer metric values we have grounded evidence for
     (Employee Count, Funding Stage, Founding Year).
  6. Skip any metric row that the analyst has already locked via override.

CLI:
    python discovery.py --seed "Tabby" --max 5
    python discovery.py --seed "Tamara" --max 3 --sources techcrunch.com,menabytes.com
    python discovery.py --seed "Rain" --max 5 --dry-run

Why a CLI and not a UI button?
  Discovery is slow (30–90s for 5 companies) and best pre-run for demos.
  The user's choice: pre-seed the DB, then show the scoring story.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import db

# ---------------------------------------------------------------------------
# Import Alpha Scout as a library.
#
# Alpha Scout isn't pip-installed — it's a sibling project in
# /Users/alexandrecela/Documents/alpha_scout. We add it to sys.path so we can
# import its modules directly. Override with ALPHA_SCOUT_PATH env var if
# you move it elsewhere.
# ---------------------------------------------------------------------------

ALPHA_SCOUT_PATH = os.environ.get(
    "ALPHA_SCOUT_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "alpha_scout"),
)

if ALPHA_SCOUT_PATH not in sys.path:
    sys.path.insert(0, ALPHA_SCOUT_PATH)

try:
    # These imports resolve against Alpha Scout's source tree above.
    from search import search_similar_companies  # type: ignore
    from source_enrichment import enrich_company  # type: ignore
    from models import SearchResult  # type: ignore
except ImportError as e:
    raise ImportError(
        f"Could not import Alpha Scout from {ALPHA_SCOUT_PATH}.\n"
        f"Set ALPHA_SCOUT_PATH in your environment to the alpha_scout folder.\n"
        f"Original error: {e}"
    )


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("discovery")


# ---------------------------------------------------------------------------
# Metric IDs (hardcoded from seed.sql) that discovery can seed.
#
# We only seed metrics where Alpha Scout produces grounded, auditable data.
# Everything else (LTV:CAC, MRR, Domain Expertise score) needs analyst
# judgment and stays NULL → will show up as a gap the Gap Agent can fill.
# ---------------------------------------------------------------------------

METRIC_EMPLOYEE_COUNT = "33333333-0000-0000-0000-000000000017"
METRIC_FOUNDING_YEAR  = "33333333-0000-0000-0000-000000000018"
METRIC_FUNDING_STAGE  = "33333333-0000-0000-0000-000000000015"  # score_1_5


# Funding stage → 1..5 score (matches seed.sql description for "Funding Stage")
# Pre-seed = earliest, Series C+ = most mature.
# Values outside this map stay NULL (better than a wrong guess).
FUNDING_STAGE_SCORE: Dict[str, float] = {
    "pre-seed":    1.0,
    "preseed":     1.0,
    "seed":        2.0,
    "series a":    3.0,
    "series-a":    3.0,
    "series b":    4.0,
    "series-b":    4.0,
    "series c":    5.0,
    "series-c":    5.0,
    "series d":    5.0,
    "growth":      5.0,
}


@dataclass
class DiscoveryResult:
    """One row of the discovery report — what happened for each candidate."""
    company_name: str
    company_id: Optional[str]
    status: str                     # "created" | "updated" | "skipped" | "error"
    grounding_score: float
    founders_added: int
    metrics_seeded: int
    message: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stage_to_score(stage: Optional[str]) -> Optional[float]:
    """Map a funding stage string to the Funding Stage (1-5) metric score.
    Returns None for unknown stages — we'd rather show a gap than guess."""
    if not stage:
        return None
    key = stage.strip().lower()
    return FUNDING_STAGE_SCORE.get(key)


def _get_locked_metric_ids(company_id: str) -> set:
    """Return metric_ids where the analyst has set override=TRUE.
    Discovery must NEVER overwrite these — they're the analyst's locked truth."""
    rows = db.get_latest_values_for_company(company_id)
    return {str(r["metric_id"]) for r in rows if r.get("override")}


def _seed_metric_if_new(
    company_id: str,
    metric_id: str,
    value: str,
    evidence_url: Optional[str],
    raw_evidence: str,
    source_id: Optional[str],
    locked_ids: set,
) -> bool:
    """Insert a metric value ONLY if the analyst hasn't locked this metric.
    Returns True if we actually wrote a row. Safe to call repeatedly."""
    if metric_id in locked_ids:
        logger.info(f"  🔒 Skipping metric {metric_id[:8]}… (analyst override)")
        return False
    db.upsert_metric_value(
        company_id=company_id,
        metric_id=metric_id,
        value=value,
        source_id=source_id,
        raw_evidence=raw_evidence,
        captured_by="alpha_scout",
        confidence=0.9,               # discovery data is strong but not analyst-verified
        override=False,
        override_reason=None,
        evidence_url=evidence_url,
        url_verified=False,           # link_verifier can confirm later
        url_verified_at=None,
    )
    return True


# ---------------------------------------------------------------------------
# Core: upsert ONE company discovered by Alpha Scout
# ---------------------------------------------------------------------------

def persist_one(
    sr: SearchResult,
    alpha_scout_source_id: str,
    dry_run: bool = False,
) -> DiscoveryResult:
    """
    Take one Alpha Scout SearchResult, enrich it, and write everything to Postgres.

    Returns a DiscoveryResult summarising what was written.
    """
    name = (sr.name or "").strip()
    if not name or name.lower() == "unknown":
        return DiscoveryResult(name or "?", None, "skipped", 0.0, 0, 0, "empty name")

    logger.info(f"→ {name} (grounding={sr.grounding_score:.0%})")

    # --- Step 1: deep enrichment (website, LinkedIn, stage) ---------------
    # SearchResult already has some fields; enrichment runs 3 finders in
    # parallel and merges everything into a CompanyEnrichment object.
    try:
        enrichment = enrich_company(
            name,
            initial_data={
                "sector": sr.sector if sr.sector != "Not Found" else None,
                "location": sr.location if sr.location != "Not Found" else None,
            },
        )
    except Exception as e:
        logger.warning(f"  enrichment failed: {e}")
        enrichment = None

    # Extract fields with safe fallbacks to SearchResult.
    # enrichment.<field> is a MultiSourceField (has .value and .sources) or None.
    def _val(field):
        return field.value if field and hasattr(field, "value") else None

    website = _val(enrichment.website_url) if enrichment else None
    website = website or (sr.website if sr.website != "Not Found" else None)
    linkedin_co = _val(enrichment.linkedin_url) if enrichment else None
    country = _val(enrichment.location) if enrichment else None
    country = country or (sr.location if sr.location != "Not Found" else None)
    industry = _val(enrichment.sector) if enrichment else None
    industry = industry or (sr.sector if sr.sector != "Not Found" else None)
    stage = _val(enrichment.funding_stage) if enrichment else None
    stage = stage or (sr.funding_stage if sr.funding_stage != "Not Found" else None)
    emp_count = _val(enrichment.employee_count) if enrichment else None
    founding_year = _val(enrichment.founded_year) if enrichment else None
    founders_raw = _val(enrichment.founders) if enrichment else None
    founders_list = _normalize_founders(founders_raw, sr.founders)

    description = _val(enrichment.description) if enrichment else None
    notes = f"Discovered by Alpha Scout. {description or ''}".strip()

    if dry_run:
        logger.info(f"  [DRY RUN] would upsert: website={website}, stage={stage}, founders={founders_list}")
        return DiscoveryResult(name, None, "skipped", sr.grounding_score, 0, 0, "dry-run")

    # --- Step 2: upsert the company ---------------------------------------
    company_row = db.upsert_company_discovery(
        name=name,
        website=website,
        linkedin_url=linkedin_co,
        country=country,
        industry=industry,
        discovery_source_url=sr.source_url or None,
        discovery_grounding_score=float(sr.grounding_score or 0.0),
        website_verified=bool(getattr(sr, "website_verified", False)),
        notes=notes,
    )
    if not company_row:
        return DiscoveryResult(name, None, "error", sr.grounding_score, 0, 0, "upsert failed")
    company_id = str(company_row["id"])

    # --- Step 3: upsert founders (UNVERIFIED — resolver runs separately) --
    founders_added = 0
    for fname in founders_list:
        if db.upsert_founder_unverified(company_id, fname):
            founders_added += 1

    # --- Step 4: seed metric values where we have evidence ----------------
    locked = _get_locked_metric_ids(company_id)
    metrics_seeded = 0

    # Employee Count
    emp_num = _parse_employee_count(emp_count)
    if emp_num is not None:
        if _seed_metric_if_new(
            company_id, METRIC_EMPLOYEE_COUNT,
            value=str(emp_num),
            evidence_url=linkedin_co,
            raw_evidence=f"LinkedIn reports {emp_count}",
            source_id=alpha_scout_source_id,
            locked_ids=locked,
        ):
            metrics_seeded += 1

    # Founding Year
    if founding_year and isinstance(founding_year, (int, str)):
        try:
            year = int(str(founding_year))
            if 1950 <= year <= 2100:
                if _seed_metric_if_new(
                    company_id, METRIC_FOUNDING_YEAR,
                    value=str(year),
                    evidence_url=linkedin_co or website,
                    raw_evidence=f"Founded in {year} per enrichment",
                    source_id=alpha_scout_source_id,
                    locked_ids=locked,
                ):
                    metrics_seeded += 1
        except (TypeError, ValueError):
            pass

    # Funding Stage → 1..5 score
    stage_score = _stage_to_score(stage)
    if stage_score is not None:
        if _seed_metric_if_new(
            company_id, METRIC_FUNDING_STAGE,
            value=str(stage_score),
            evidence_url=sr.source_url or None,
            raw_evidence=f"Funding stage '{stage}' → score {stage_score}",
            source_id=alpha_scout_source_id,
            locked_ids=locked,
        ):
            metrics_seeded += 1

    status = "created" if not company_row.get("website") or company_row.get("discovered_at") else "updated"
    return DiscoveryResult(
        company_name=name,
        company_id=company_id,
        status=status,
        grounding_score=sr.grounding_score,
        founders_added=founders_added,
        metrics_seeded=metrics_seeded,
        message=f"OK ({founders_added} founders, {metrics_seeded} metrics)",
    )


def _normalize_founders(enriched, fallback: List[str]) -> List[str]:
    """Enrichment returns founders as a string, list, or None.
    Normalise to a clean list of names, falling back to SearchResult list."""
    if enriched:
        if isinstance(enriched, list):
            return [str(f).strip() for f in enriched if str(f).strip()]
        if isinstance(enriched, str):
            return [f.strip() for f in enriched.split(",") if f.strip()]
    return [f for f in (fallback or []) if f]


def _parse_employee_count(raw) -> Optional[int]:
    """Accept int, '42', or '11-50' ranges. For ranges, return the LOWER bound."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    s = str(raw).strip()
    if not s:
        return None
    # Range form "11-50" → 11
    import re
    nums = re.findall(r"\d+", s)
    if nums:
        try:
            return int(nums[0])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def discover_and_persist(
    seed: str,
    max_results: int = 5,
    sources: Optional[List[str]] = None,
    dry_run: bool = False,
) -> List[DiscoveryResult]:
    """
    Run the full discovery pipeline for one seed company.

    The seed is either:
      - A name in Alpha Scout's PORTFOLIO_COMPANIES config (best match quality)
      - Any string — Alpha Scout will search "similar to <seed>" on trusted sources

    Returns a list of DiscoveryResult (one per candidate), ready to print.
    """
    logger.info(f"🔎 Discovery: seed='{seed}', max={max_results}, sources={sources or 'default'}")

    # Ensure an "Alpha Scout" row exists in data_source so metric values can
    # attribute back to it in the Table Browser.
    alpha_scout_source_id = db.get_or_create_data_source("Alpha Scout", source_type="Platform")

    # --- Alpha Scout search ---------------------------------------------
    try:
        search_results = search_similar_companies(
            seed=seed,
            max_results=max_results,
            sources=sources,
        )
    except Exception as e:
        logger.error(f"Alpha Scout search failed: {e}")
        return [DiscoveryResult(seed, None, "error", 0.0, 0, 0, f"search failed: {e}")]

    logger.info(f"  {len(search_results)} candidates passed grounding.")

    # --- Persist each candidate -----------------------------------------
    results: List[DiscoveryResult] = []
    for sr in search_results:
        try:
            results.append(persist_one(sr, alpha_scout_source_id, dry_run=dry_run))
        except Exception as e:
            logger.exception(f"Failed to persist {sr.name}: {e}")
            results.append(DiscoveryResult(sr.name, None, "error", sr.grounding_score, 0, 0, str(e)))

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Discover companies via Alpha Scout and persist to Postgres.",
    )
    parser.add_argument("--seed", required=True, help="Seed company name (e.g. 'Tabby')")
    parser.add_argument("--max", type=int, default=5, help="Max companies to return (default: 5)")
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated list of trusted domains to search (default: Alpha Scout's curated list)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the search + enrichment but do NOT write to the database.",
    )
    args = parser.parse_args()

    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    results = discover_and_persist(
        seed=args.seed,
        max_results=args.max,
        sources=sources,
        dry_run=args.dry_run,
    )

    # Print a clean summary table
    print("\n" + "=" * 78)
    print(f"Discovery report for seed='{args.seed}' ({len(results)} candidates)")
    print("=" * 78)
    print(f"{'Status':<10} {'Grounding':<10} {'Founders':<9} {'Metrics':<8} Company")
    print("-" * 78)
    for r in results:
        print(
            f"{r.status:<10} "
            f"{r.grounding_score:<10.0%} "
            f"{r.founders_added:<9} "
            f"{r.metrics_seeded:<8} "
            f"{r.company_name}"
        )
    print("=" * 78)
    ok = sum(1 for r in results if r.status in ("created", "updated"))
    print(f"✅ {ok}/{len(results)} persisted")


if __name__ == "__main__":
    main()
