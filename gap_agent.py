"""
Gap Agent — closes missing must-have metrics.

For each missing must-have metric on a company, the agent routes to one of
two strategies based on the metric's obtain_method:

  - "Ask Founders"         → Type A: draft an email asking the founder
                             (we never auto-send — analyst reviews the draft)
  - "Tavily" / "LinkedIn"  → Type B: Tavily web search + Gemini extract
                             + link_verifier (same pattern as market_agent)
  - anything else           → skip, log as 'manual_required'

Every attempt is logged in `gap_action` so the scorecard UI can show
pending drafts and the analyst can mark them sent/resolved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import db
import llm_client
import link_verifier
import scorer

logger = logging.getLogger(__name__)

# Which obtain_methods route to Type B (web search) vs Type A (outreach)
_WEB_METHODS     = {"Tavily", "LinkedIn", "Web Search"}
_FOUNDER_METHODS = {"Ask Founders"}


# =============================================================================
# Type A — Founder outreach (drafts an email)
# =============================================================================

_OUTREACH_PROMPT = """You are a venture capital analyst drafting a concise
email to a startup founder to request ONE missing data point we need to
complete our investment evaluation.

COMPANY:       {company_name}
INDUSTRY:      {industry}
MISSING METRIC: {metric_name}
DESCRIPTION:   {metric_description}

Rules:
- Warm but professional tone. Maximum 6 short sentences.
- Open with one line acknowledging their company (no generic flattery).
- Be specific about what number/data we need and why it matters to us.
- Close with a clear ask and a friendly sign-off from "The investment team".
- Do NOT invent any facts about the company. Do NOT include placeholders
  like [FOUNDER NAME] — use "Hi there," if you don't know the name.
- Return the email body ONLY. No subject line, no JSON, no markdown.
"""


def _draft_outreach_email(company: Dict, missing: Dict) -> str:
    """Ask Gemini to draft a short outreach email for one missing metric."""
    prompt = _OUTREACH_PROMPT.format(
        company_name=company.get("name", "your company"),
        industry=company.get("industry", "your sector"),
        metric_name=missing["metric_name"],
        metric_description=missing.get("description", "(no description available)"),
    )
    return llm_client.call_gemini(
        prompt=prompt,
        span_name="gap_agent_outreach_draft",
        metadata={
            "company_id": company.get("id"),
            "metric": missing["metric_name"],
        },
        temperature=0.3,  # some variety in phrasing but still focused
    ).strip()


# =============================================================================
# Type B — Web search (Tavily + Gemini + link verification)
# =============================================================================

_WEB_EXTRACT_PROMPT = """You are filling in a missing data point for a
startup from web search results.

COMPANY:       {company_name}
WEBSITE:       {website}
INDUSTRY:      {industry}
MISSING METRIC: {metric_name}
DESCRIPTION:   {metric_description}
EXPECTED TYPE: {value_type}
TYPE RULE:     {type_rule}

SEARCH RESULTS:
{search_results}

TASK:
Extract the value of the missing metric for THIS specific company.

Rules:
1. Return ONLY valid JSON:
   {{
     "value": "<string obeying the TYPE RULE>",
     "source_url": "<URL of the source>",
     "evidence_quote": "<exact quote from the source, max 200 chars>",
     "confidence": <float 0-1>
   }}
2. The evidence_quote MUST be verbatim from one of the search results.
3. The source must clearly be about THIS company (name match) — not a
   different company with a similar name.
4. If no credible match exists, return:
   {{"value": null, "source_url": null, "evidence_quote": null, "confidence": 0.0}}

Return JSON only — no prose, no markdown fences.
"""


_VALUE_TYPE_RULES = {
    "boolean":   'Must be exactly "true" or "false".',
    "score_1_5": 'Must be a digit "1"–"5".',
    "number":    'Numeric string, no units (e.g. "18.5").',
    "text":      'Short phrase, max 60 chars.',
}


def _tavily_query(company: Dict, missing: Dict) -> str:
    """Build a focused search query for this company + metric."""
    # Put the company name first — Tavily gives huge weight to the first terms.
    return f'{company["name"]} {missing["metric_name"]} {company.get("website","")}'


def _search_web(company: Dict, missing: Dict) -> Dict:
    """Run Tavily + Gemini for one web-findable metric. Returns extraction dict or None."""
    # Lazy-import so tavily isn't required if no web gaps exist
    from market_agent import _tavily_search  # reuse existing wrapper

    results = _tavily_search(_tavily_query(company, missing), max_results=5)
    if not results:
        return None

    formatted = "\n\n".join(
        f"[{i+1}] {r.get('title','')}\nURL: {r.get('url','')}\n"
        f"CONTENT: {r.get('content','')[:800]}"
        for i, r in enumerate(results)
    )

    value_type = missing.get("value_type", "text")
    prompt = _WEB_EXTRACT_PROMPT.format(
        company_name=company.get("name", ""),
        website=company.get("website", ""),
        industry=company.get("industry", ""),
        metric_name=missing["metric_name"],
        metric_description=missing.get("description", ""),
        value_type=value_type,
        type_rule=_VALUE_TYPE_RULES.get(value_type, "Short string."),
        search_results=formatted,
    )

    raw = llm_client.call_gemini(
        prompt=prompt,
        span_name="gap_agent_web_extract",
        metadata={
            "company_id": company.get("id"),
            "metric": missing["metric_name"],
        },
        temperature=0.0,
    )
    parsed = llm_client.parse_json_response(raw)
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    return parsed if isinstance(parsed, dict) else None


def _metric_value_type(metric_id: str) -> str:
    """Look up the value_type for a metric (scorer.get_missing_must_haves doesn't include it)."""
    row = db._fetchone(
        "SELECT value_type FROM metric WHERE id = %s", (metric_id,)
    )
    return row["value_type"] if row else "text"


# =============================================================================
# Dispatch
# =============================================================================

def process_gap(company: Dict, missing: Dict) -> Dict:
    """
    Process ONE missing must-have. Returns a result dict describing what
    happened: {status, message, gap_type, action_id?, value_written?, ...}.
    """
    method = missing.get("obtain_method", "")
    metric_id = missing["metric_id"]
    company_id = company["id"]

    # -------- Type A: founder outreach -----------------------------------
    if method in _FOUNDER_METHODS:
        try:
            email = _draft_outreach_email(company, missing)
        except Exception as e:
            return {"status": "error", "gap_type": "outreach",
                    "message": f"Drafting failed: {e}"}

        action = db.log_gap_action(
            company_id=company_id,
            metric_id=metric_id,
            gap_type="outreach",
            output_draft=email,
            status="pending",   # analyst will mark 'sent' after reviewing
        )
        return {
            "status": "drafted",
            "gap_type": "outreach",
            "message": f"Drafted outreach email for {missing['metric_name']}",
            "action_id": action.get("id"),
            "draft": email,
        }

    # -------- Type B: web search -----------------------------------------
    if method in _WEB_METHODS:
        # Inject value_type into missing dict for the prompt
        missing_with_type = dict(missing)
        missing_with_type["value_type"] = _metric_value_type(metric_id)

        try:
            extracted = _search_web(company, missing_with_type)
        except Exception as e:
            return {"status": "error", "gap_type": "search",
                    "message": f"Web search failed: {e}"}

        if not extracted or not extracted.get("value") \
                or not extracted.get("source_url") \
                or not extracted.get("evidence_quote") \
                or (extracted.get("confidence") or 0) < 0.4:
            # Log a 'failed' gap_action so the UI shows we tried
            action = db.log_gap_action(
                company_id=company_id, metric_id=metric_id,
                gap_type="search", output_draft="No confident match found.",
                status="failed",
            )
            return {"status": "no_data", "gap_type": "search",
                    "message": "No confident value found via web search",
                    "action_id": action.get("id")}

        # Verify the URL deterministically
        url_ok, verify_reason = link_verifier.verify(
            extracted["source_url"], extracted["evidence_quote"]
        )

        # Write the new metric value
        db.upsert_metric_value(
            company_id=company_id,
            metric_id=metric_id,
            value=str(extracted["value"]),
            source_id=None,
            raw_evidence=extracted["evidence_quote"],
            captured_by="gap_agent",
            confidence=float(extracted.get("confidence", 0.7)),
            override=False,
            override_reason=None,
            evidence_url=extracted["source_url"],
            url_verified=url_ok,
            url_verified_at=datetime.now(timezone.utc).isoformat(),
        )

        # Log the action as resolved (we actually filled the gap)
        action = db.log_gap_action(
            company_id=company_id, metric_id=metric_id,
            gap_type="search",
            output_draft=(
                f"Value: {extracted['value']}\n"
                f"Source: {extracted['source_url']}\n"
                f"Quote: {extracted['evidence_quote']}\n"
                f"Verified: {url_ok} ({verify_reason})"
            ),
            status="resolved",
        )
        return {
            "status": "filled",
            "gap_type": "search",
            "message": f"Filled {missing['metric_name']} = {extracted['value']}",
            "action_id": action.get("id"),
            "value_written": extracted["value"],
            "source_url": extracted["source_url"],
            "url_verified": url_ok,
            "url_verify_reason": verify_reason,
        }

    # -------- Unknown method --------------------------------------------
    return {
        "status": "skipped",
        "gap_type": "manual_required",
        "message": f"No automated strategy for obtain_method={method!r}",
    }


def process_company(company_id: str) -> List[Dict]:
    """
    Run the gap agent across all missing must-haves for a company.
    Returns a list of result dicts, one per gap processed.
    """
    company = db.get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    gaps = scorer.get_missing_must_haves(company_id)
    logger.info(f"Gap agent: processing {len(gaps)} gap(s) for {company.get('name')}")

    results = []
    for gap in gaps:
        try:
            results.append(process_gap(company, gap))
        except Exception as e:
            logger.exception("Unexpected error processing gap")
            results.append({
                "status": "error",
                "gap_type": "unknown",
                "message": str(e),
            })
    return results
