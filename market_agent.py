"""
Market Signal Agent — refreshes stale "Market Growth" metric values.

Flow:
  1. Detect companies whose Market Growth metrics haven't been updated in N days.
  2. For each, run a Tavily web search for the company's industry + "market size 2026".
  3. Feed the top results to Gemini, which extracts a numeric CAGR value with a
     quoted evidence snippet and source URL.
  4. Write a new `company_metric_value` row (old row auto-flipped to is_latest=FALSE).

Safety rails:
  - NEVER overwrites rows where `override = TRUE` (analyst lock).
  - Every value must include a source URL and a quoted evidence snippet.
  - If Gemini cannot confidently extract a number, we log a gap_action instead of
    writing a bogus value.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import db
import llm_client
import link_verifier

logger = logging.getLogger(__name__)

# How old a Market Growth value has to be before we consider it stale.
# 180 days = roughly two quarters. Market data moves fast but not daily.
STALENESS_DAYS = 180

# The metric type we refresh. Name is defined in seed.sql.
MARKET_GROWTH_TYPE_NAME = "Market Growth"


# ---------------------------------------------------------------------------
# Tavily search wrapper
# ---------------------------------------------------------------------------

def _tavily_search(query: str, max_results: int = 5) -> List[Dict]:
    """
    Run a Tavily web search. Returns a list of {title, url, content} dicts.

    Why Tavily instead of plain Google: Tavily pre-filters low-quality sites and
    returns clean text snippets ready for LLM consumption. No HTML scraping needed.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY not set in .env")

    # Lazy import so the app can start without tavily installed during dev
    from tavily import TavilyClient
    client = TavilyClient(api_key=api_key)

    response = client.search(
        query=query,
        search_depth="advanced",   # deeper, more expensive but better for facts
        max_results=max_results,
        include_answer=False,
    )
    return response.get("results", [])


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

def find_stale_market_values(days: int = STALENESS_DAYS) -> List[Dict]:
    """
    Return a list of company_metric_value rows for Market Growth metrics
    that are older than `days` and NOT overridden by an analyst.

    This is what the demo surfaces as "stale market signal" on Climate C.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db._fetchall("""
        SELECT
            cmv.id              AS value_id,
            cmv.company_id      AS company_id,
            cmv.metric_id       AS metric_id,
            cmv.value           AS current_value,
            cmv.captured_at     AS captured_at,
            c.name              AS company_name,
            c.industry          AS company_industry,
            m.name              AS metric_name,
            m.value_type        AS value_type
        FROM company_metric_value cmv
        JOIN company c     ON c.id = cmv.company_id
        JOIN metric m      ON m.id = cmv.metric_id
        JOIN metric_type mt ON mt.id = m.metric_type_id
        WHERE cmv.is_latest = TRUE
          AND cmv.override   = FALSE
          AND mt.name        = %s
          AND cmv.captured_at < %s
        ORDER BY cmv.captured_at ASC
    """, (MARKET_GROWTH_TYPE_NAME, cutoff))
    return rows


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_VALUE_TYPE_INSTRUCTIONS = {
    "boolean":   'Must be exactly "true" or "false" (lowercase string).',
    "score_1_5": 'Must be a string digit "1" through "5".',
    "number":    'Must be a numeric string with no units (e.g. "18.5" not "18.5%").',
    "text":      'Short descriptive phrase, max 60 chars.',
}


_EXTRACT_PROMPT = """You are extracting a single market-growth metric from web search results.

METRIC:           {metric_name}
INDUSTRY:         {industry}
EXPECTED TYPE:    {value_type}
TYPE RULE:        {type_rule}

SEARCH RESULTS:
{search_results}

TASK:
Extract the most recent, credible value for the metric above.

Rules:
1. Return ONLY valid JSON matching this exact schema:
   {{
     "value": "<string — MUST obey the TYPE RULE above>",
     "score_1_5": <integer 1-5, where 5 = excellent market dynamics, 1 = poor>,
     "source_url": "<the URL of the source>",
     "evidence_quote": "<exact quote from the source supporting the value, max 200 chars>",
     "confidence": <float 0-1, how confident you are in this extraction>
   }}
2. The evidence_quote MUST appear verbatim in one of the search results' content.
3. If the TYPE RULE cannot be satisfied from the evidence, return:
   {{"value": null, "score_1_5": null, "source_url": null, "evidence_quote": null, "confidence": 0.0}}
4. Do NOT invent numbers. Do NOT combine numbers from different sources.

Return the JSON only — no prose, no markdown fences.
"""


def _extract_with_gemini(metric_name: str, industry: str, value_type: str, search_results: List[Dict]) -> Dict:
    """
    Ask Gemini to extract a structured value from Tavily search results.
    Returns a dict with keys: value, score_1_5, source_url, evidence_quote, confidence.
    """
    # Format search results as numbered snippets — keeps the prompt tidy
    formatted = "\n\n".join(
        f"[{i+1}] {r.get('title','(no title)')}\n"
        f"URL: {r.get('url','')}\n"
        f"CONTENT: {r.get('content','')[:800]}"
        for i, r in enumerate(search_results)
    )

    prompt = _EXTRACT_PROMPT.format(
        metric_name=metric_name,
        industry=industry,
        value_type=value_type,
        type_rule=_VALUE_TYPE_INSTRUCTIONS.get(value_type, "Short string."),
        search_results=formatted,
    )

    raw = llm_client.call_gemini(
        prompt=prompt,
        span_name="market_agent_extract",
        metadata={"metric_name": metric_name, "industry": industry},
        temperature=0.0,
    )
    parsed = llm_client.parse_json_response(raw)
    if not isinstance(parsed, dict):
        # parse_json_response may return a list if Gemini wrapped in an array
        parsed = parsed[0] if isinstance(parsed, list) and parsed else {}
    return parsed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def refresh_one(stale_row: Dict) -> Dict:
    """
    Refresh a single stale market metric.

    Returns a result dict with keys:
      - status: 'updated' | 'no_data' | 'error'
      - message: human-readable outcome
      - new_value: the value written (if status='updated')
      - source_url, evidence_quote: provenance (if status='updated')
    """
    industry = stale_row.get("company_industry") or "technology"
    metric_name = stale_row["metric_name"]

    # Step 1: build a targeted web query
    query = f"{industry} industry market size growth CAGR 2025 2026"

    try:
        results = _tavily_search(query, max_results=5)
    except Exception as e:
        return {"status": "error", "message": f"Tavily search failed: {e}"}

    if not results:
        return {"status": "no_data", "message": "No search results returned."}

    # Step 2: extract with Gemini (pass value_type so it returns correct shape)
    try:
        extracted = _extract_with_gemini(
            metric_name=metric_name,
            industry=industry,
            value_type=stale_row.get("value_type", "text"),
            search_results=results,
        )
    except Exception as e:
        return {"status": "error", "message": f"Gemini extraction failed: {e}"}

    # Step 3: validate — require value + evidence + source
    if (
        not extracted.get("value")
        or not extracted.get("evidence_quote")
        or not extracted.get("source_url")
        or (extracted.get("confidence") or 0) < 0.4
    ):
        return {
            "status": "no_data",
            "message": "Gemini could not extract a confident value with evidence.",
        }

    # Step 4: find or create a data_source row for this URL
    source_name = "Web Press Articles"  # from our seed data
    source = db.get_source_by_name(source_name)
    source_id = source["id"] if source else None

    evidence_quote = extracted["evidence_quote"]
    source_url = extracted["source_url"]

    # Step 5: verify the URL BEFORE writing — deterministic check that the
    # quote actually appears at that URL (guards against hallucinated links).
    # We still write the row even if verification fails, but flag it unverified
    # so the UI can warn the analyst.
    url_ok, verify_reason = link_verifier.verify(source_url, evidence_quote)

    # Step 6: write new company_metric_value row
    # upsert_metric_value demotes the old is_latest row and inserts a new one.
    # If the current row is override=True, find_stale_market_values already skipped it.
    new_row = db.upsert_metric_value(
        company_id=stale_row["company_id"],
        metric_id=stale_row["metric_id"],
        value=str(extracted["value"]),
        source_id=source_id,
        raw_evidence=evidence_quote,        # quote only — URL goes in its own column
        captured_by="market_agent",
        confidence=float(extracted.get("confidence", 0.7)),
        override=False,
        override_reason=None,
        evidence_url=source_url,
        url_verified=url_ok,
        # Passing None lets Postgres use NOW() default if we haven't verified;
        # we always verify here so set to NOW() explicitly via SQL NOW() is not
        # possible through the param — use datetime.
        url_verified_at=datetime.now(timezone.utc).isoformat(),
    )

    return {
        "status": "updated",
        "message": f"Refreshed {metric_name} for {stale_row['company_name']}",
        "new_value": extracted["value"],
        "source_url": source_url,
        "evidence_quote": evidence_quote,
        "url_verified": url_ok,
        "url_verify_reason": verify_reason,
        "row": new_row,
    }


def refresh_all_stale(days: int = STALENESS_DAYS) -> List[Dict]:
    """
    Run the market agent across every stale Market Growth value in the DB.
    Returns a list of result dicts (one per attempt).
    """
    stale = find_stale_market_values(days=days)
    logger.info(f"Market agent: found {len(stale)} stale values")
    return [refresh_one(row) for row in stale]
