"""
company_intel — per-company context: news fetching + share summary builder.

News flow:
  1. fetch_news(company_id)      → Tavily search, writes to news_article cache
  2. get_news(company_id)        → reads cache; triggers a refresh if older
                                   than NEWS_MAX_AGE_DAYS days

Share flow:
  - build_share_summary(...)     → returns a markdown brief the analyst can
                                   paste into Slack/email
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

import db

logger = logging.getLogger(__name__)

# How long cached news is considered fresh. 7 days matches the step plan.
NEWS_MAX_AGE_DAYS = 7

# How many articles to keep per fetch. Tavily returns 5 by default for news.
_TAVILY_MAX_RESULTS = 5


# =============================================================================
# News fetching (Tavily)
# =============================================================================

def _tavily_news_search(query: str, max_results: int = _TAVILY_MAX_RESULTS) -> List[Dict]:
    """
    Call Tavily with topic='news' to get recent articles.
    Returns a list of {title, url, content, published_date} dicts.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")

    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "topic": "news",                 # prioritize news outlets
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", []) or []


def fetch_news(company_id: str) -> int:
    """
    Run a fresh news search for a company and upsert results into the cache.
    Returns the number of articles stored/updated.
    """
    company = db.get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    # Build a targeted query — put the company name first so Tavily weights it.
    # Include industry so a generic "Nova" search doesn't pull unrelated news.
    query = f'{company["name"]} {company.get("industry","")} startup funding news'.strip()
    logger.info(f"fetch_news query: {query!r}")

    try:
        results = _tavily_news_search(query)
    except Exception as e:
        logger.exception("Tavily news fetch failed")
        raise RuntimeError(f"News fetch failed: {e}")

    count = 0
    for r in results:
        url = r.get("url")
        title = r.get("title")
        if not url or not title:
            continue
        db.upsert_news_article(
            company_id=company_id,
            title=title,
            url=url,
            snippet=(r.get("content") or "")[:500],
            published_at=r.get("published_date"),
        )
        count += 1
    return count


def get_news(
    company_id: str,
    max_age_days: int = NEWS_MAX_AGE_DAYS,
    auto_refresh: bool = False,
) -> List[Dict]:
    """
    Return cached news for a company. If `auto_refresh` is True and the cache
    is older than `max_age_days` (or empty), trigger a fetch first.
    """
    if auto_refresh:
        last = db.newest_news_fetch(company_id) or {}
        last_fetch = last.get("last_fetch")
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if last_fetch is None or last_fetch < cutoff:
            try:
                fetch_news(company_id)
            except Exception:
                # Swallow — return whatever stale cache we have
                pass
    return db.get_cached_news(company_id)


# =============================================================================
# Pipeline stage transitions (thin wrapper for UI clarity)
# =============================================================================

# Valid pipeline stages (must match seed / product vocab)
VALID_STAGES = [
    "deal_sourcing",
    "first_contact",
    "due_diligence",
    "ic_review",
    "invested",
    "passed",
]


def transition_stage(
    company_id: str,
    new_stage: str,
    user_id: str,
    score_snapshot: Optional[float] = None,
) -> Dict:
    """Move a company to a new pipeline stage + log the transition."""
    if new_stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage {new_stage!r}. Must be one of {VALID_STAGES}")
    return db.update_company_stage(
        company_id=company_id,
        new_stage=new_stage,
        changed_by=user_id,
        score_snapshot=score_snapshot,
        triggered_by="manual",
    )


# =============================================================================
# Share summary (markdown)
# =============================================================================

def build_share_summary(
    company: Dict,
    scorecard,          # scorer.CompanyScorecard — avoid circular import
    founders: List[Dict],
    news: List[Dict],
) -> str:
    """
    Build a markdown summary an analyst can copy-paste into Slack/email.
    Intentionally plain markdown — Slack renders it, and it's email-safe.
    """
    lines = []
    lines.append(f"# {company['name']} — scorecard brief")
    lines.append("")
    # Headline number
    if scorecard.overall_score is not None:
        lines.append(f"**Overall score:** {scorecard.overall_score:.2f} / 5  ·  "
                     f"stage: `{company.get('pipeline_stage','?')}`")
    else:
        missing = ", ".join(scorecard.missing_must_have_types) or "n/a"
        lines.append(f"**Overall score:** _incomplete_ — missing must-haves: {missing}  ·  "
                     f"stage: `{company.get('pipeline_stage','?')}`")
    lines.append("")

    # Basics
    lines.append(f"- **Industry:** {company.get('industry','?')}")
    lines.append(f"- **Country:** {company.get('country','?')}")
    if company.get("website"):
        lines.append(f"- **Website:** {company['website']}")
    if company.get("linkedin_url"):
        lines.append(f"- **LinkedIn:** {company['linkedin_url']}")

    # Founders
    if founders:
        lines.append("")
        lines.append("## Founders")
        for f in founders:
            lurl = f" — {f['linkedin_url']}" if f.get("linkedin_url") else ""
            lines.append(f"- **{f['name']}** ({f.get('title','')}){lurl}")

    # Dimension scores
    lines.append("")
    lines.append("## Dimension scores")
    for ts in scorecard.type_scores:
        score_str = f"{ts.type_score:.2f}" if ts.type_score is not None else "—"
        marker = "⭐" if ts.must_have else "  "
        lines.append(f"- {marker} **{ts.metric_type_name}:** {score_str}")

    # Recent news (top 3)
    if news:
        lines.append("")
        lines.append("## Recent news")
        for n in news[:3]:
            lines.append(f"- [{n['title']}]({n['url']})")

    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    return "\n".join(lines)
