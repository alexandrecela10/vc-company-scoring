"""
Founder LinkedIn Resolver — find + verify founders' personal LinkedIn profiles.

Why this module exists separately from discovery.py:
  Resolving a person's LinkedIn is the single most hallucination-prone step
  (same name, wrong person). We run it as a distinct pass with a strict
  verification rule: the LinkedIn snippet MUST mention the company name
  or its domain, otherwise we store the URL but mark verified=FALSE so the
  UI shows a ⚠️ badge. We never silently guess.

How it works:
  1. Tavily query: "<founder>" "<company>" site:linkedin.com/in/
  2. Take the first result whose URL matches the /in/ pattern (person page).
  3. Check snippet/title for company name OR domain → verified=TRUE/FALSE.
  4. Write back to the founder row via db.mark_founder_linkedin_verified.

CLI:
    python founder_linkedin.py --all                    # resolve every unverified founder
    python founder_linkedin.py --company-id <uuid>      # just one company's founders
    python founder_linkedin.py --dry-run                # print what would happen, no writes
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from tavily import TavilyClient

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("founder_linkedin")


# Max founders resolved in parallel. Keep modest so Tavily rate limits don't bite.
# 4 is a safe default — matches Alpha Scout's own enrichment concurrency.
MAX_PARALLEL = 4


@dataclass
class ResolveResult:
    """One row of the resolver report — what happened for each founder."""
    founder_id: str
    name: str
    company_name: str
    status: str              # "verified" | "unverified" | "not_found" | "skipped" | "error"
    linkedin_url: Optional[str] = None
    source_url: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Tavily client
# ---------------------------------------------------------------------------

def _get_tavily_client() -> TavilyClient:
    """Create a Tavily client. Raises ValueError if the key is missing."""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY not set — add it to your .env file.")
    return TavilyClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Verification logic
# ---------------------------------------------------------------------------

# LinkedIn person URLs always look like https://www.linkedin.com/in/<handle>[/...]
# This regex lets us reject company pages (/company/...) or news articles that
# happen to mention LinkedIn.
_LINKEDIN_PERSON_RE = re.compile(r"linkedin\.com/in/[^/?#]+", re.IGNORECASE)


def _is_linkedin_person_url(url: str) -> bool:
    """True if the URL is a LinkedIn /in/ personal profile, not a /company/ page."""
    return bool(_LINKEDIN_PERSON_RE.search(url or ""))


def _domain_root(url: Optional[str]) -> Optional[str]:
    """Extract the registrable domain (fintech-a from https://fintech-a.example/team)."""
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host.split(".")[0] if host else None
    except Exception:
        return None


def _snippet_mentions_company(
    snippet: str,
    title: str,
    company_name: str,
    company_website: Optional[str],
) -> bool:
    """
    Return True if the LinkedIn snippet/title mentions the company.

    We accept either:
      - company name appears (case-insensitive, word-boundary tolerant)
      - company domain root appears (e.g. "fintech-a" for fintech-a.example)
    This dual check catches founders who list the brand in their headline
    but also founders who only link out via the website.
    """
    haystack = f"{title or ''}\n{snippet or ''}".lower()
    name_norm = (company_name or "").lower().strip()

    # Try the company name first. Use a relaxed substring match — LinkedIn
    # snippets are short, and word boundaries on names like "Tabby" can
    # fail due to punctuation.
    if name_norm and name_norm in haystack:
        return True

    # Fall back to the domain root from the company's website.
    root = _domain_root(company_website)
    if root and len(root) >= 4 and root in haystack:
        return True

    return False


# ---------------------------------------------------------------------------
# Core: resolve ONE founder
# ---------------------------------------------------------------------------

def resolve_one(
    founder_id: str,
    founder_name: str,
    company_name: str,
    company_website: Optional[str],
    tavily: TavilyClient,
    dry_run: bool = False,
) -> ResolveResult:
    """
    Resolve a single founder's LinkedIn with verification.

    Outcomes (status field):
      - "verified"   → URL stored, snippet mentions the company
      - "unverified" → URL stored, snippet did NOT mention the company (⚠️ UI badge)
      - "not_found"  → Tavily returned zero /in/ pages; nothing written
      - "error"      → Tavily call failed
    """
    query = f'"{founder_name}" "{company_name}" site:linkedin.com/in/'

    try:
        raw = tavily.search(
            query=query,
            search_depth="basic",
            max_results=3,
            include_domains=["linkedin.com"],
        )
    except Exception as e:
        logger.warning(f"  Tavily failed for {founder_name}: {e}")
        return ResolveResult(
            founder_id, founder_name, company_name,
            status="error", reason=str(e),
        )

    # Pick the first result that is a /in/ personal page.
    candidate = None
    for r in raw.get("results", []):
        if _is_linkedin_person_url(r.get("url", "")):
            candidate = r
            break

    if candidate is None:
        return ResolveResult(
            founder_id, founder_name, company_name,
            status="not_found",
            reason="No /in/ result from Tavily",
        )

    url = candidate["url"]
    verified = _snippet_mentions_company(
        snippet=candidate.get("content", ""),
        title=candidate.get("title", ""),
        company_name=company_name,
        company_website=company_website,
    )
    status = "verified" if verified else "unverified"

    if not dry_run:
        db.mark_founder_linkedin_verified(
            founder_id=founder_id,
            linkedin_url=url,
            verified=verified,
            source_url=url,       # for LinkedIn, the source URL IS the profile URL
        )

    return ResolveResult(
        founder_id, founder_name, company_name,
        status=status,
        linkedin_url=url,
        source_url=url,
        reason=(
            "Snippet mentions company"
            if verified
            else "Snippet did NOT mention company — stored as unverified"
        ),
    )


# ---------------------------------------------------------------------------
# Batch resolver
# ---------------------------------------------------------------------------

def _select_unverified_founders(company_id: Optional[str] = None) -> List[Dict]:
    """
    Return every founder row whose LinkedIn is missing or unverified,
    joined with the company name + website so we can search + verify.
    """
    if company_id:
        sql = """
            SELECT f.id, f.name AS founder_name, f.company_id,
                   c.name AS company_name, c.website AS company_website
            FROM founder f JOIN company c ON c.id = f.company_id
            WHERE f.company_id = %s
              AND (f.linkedin_verified = FALSE OR f.linkedin_url IS NULL)
            ORDER BY c.name, f.name
        """
        return db._fetchall(sql, (company_id,))

    sql = """
        SELECT f.id, f.name AS founder_name, f.company_id,
               c.name AS company_name, c.website AS company_website
        FROM founder f JOIN company c ON c.id = f.company_id
        WHERE f.linkedin_verified = FALSE OR f.linkedin_url IS NULL
        ORDER BY c.name, f.name
    """
    return db._fetchall(sql)


def resolve_many(
    company_id: Optional[str] = None,
    dry_run: bool = False,
    max_parallel: int = MAX_PARALLEL,
) -> List[ResolveResult]:
    """
    Resolve every unverified founder (optionally filtered to one company).

    Runs up to `max_parallel` Tavily queries concurrently — founders don't
    depend on each other, and Tavily tolerates modest concurrency well.
    """
    founders = _select_unverified_founders(company_id)
    logger.info(f"🔍 Resolving {len(founders)} unverified founder(s)")
    if not founders:
        return []

    tavily = _get_tavily_client()
    results: List[ResolveResult] = []

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                resolve_one,
                founder_id=str(f["id"]),
                founder_name=f["founder_name"],
                company_name=f["company_name"],
                company_website=f.get("company_website"),
                tavily=tavily,
                dry_run=dry_run,
            ): f
            for f in founders
        }
        for fut in as_completed(futures):
            f = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = ResolveResult(
                    str(f["id"]), f["founder_name"], f["company_name"],
                    status="error", reason=str(e),
                )
            icon = {
                "verified":   "✅",
                "unverified": "⚠️",
                "not_found":  "❌",
                "error":      "💥",
                "skipped":    "⏭️",
            }.get(r.status, "•")
            logger.info(f"  {icon} {r.company_name} — {r.name}: {r.status} ({r.reason})")
            results.append(r)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Resolve and verify founders' LinkedIn profiles via Tavily.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true",
        help="Resolve every unverified founder across all companies.",
    )
    group.add_argument(
        "--company-id", type=str,
        help="Resolve only this company's unverified founders.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do the searches but do NOT write results to the database.",
    )
    args = parser.parse_args()

    results = resolve_many(
        company_id=args.company_id if not args.all else None,
        dry_run=args.dry_run,
    )

    # Summary table
    print("\n" + "=" * 78)
    print(f"Founder LinkedIn resolver — {len(results)} founders processed")
    print("=" * 78)
    print(f"{'Status':<12} {'Company':<22} {'Founder':<22} Reason")
    print("-" * 78)
    for r in results:
        print(
            f"{r.status:<12} "
            f"{r.company_name[:20]:<22} "
            f"{r.name[:20]:<22} "
            f"{r.reason[:30]}"
        )
    print("=" * 78)
    verified = sum(1 for r in results if r.status == "verified")
    unverified = sum(1 for r in results if r.status == "unverified")
    print(f"✅ {verified} verified · ⚠️ {unverified} unverified · {len(results) - verified - unverified} other")


if __name__ == "__main__":
    main()
