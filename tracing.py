"""
Langfuse Tracing Setup — centralized observability for Alpha Scout.

Mirrors the BDC Footnotes pattern: a singleton Langfuse client with helpers
for creating traces (logical groupings) and generations (individual LLM calls).

Every LLM call in the app flows through here, giving us:
- Full prompt/response visibility in the Langfuse dashboard
- Latency, token usage, and cost tracking
- "Faithfulness" scoring to catch hallucinations

Required environment variables:
    LANGFUSE_SECRET_KEY  — your Langfuse secret key
    LANGFUSE_PUBLIC_KEY  — your Langfuse public key
    LANGFUSE_HOST        — Langfuse server URL (defaults to https://cloud.langfuse.com)
"""

import os
import logging
from langfuse import Langfuse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton — one Langfuse client for the entire app lifetime.
# Why a singleton? Creating multiple clients wastes connections and can
# cause duplicate events. One client batches everything efficiently.
# ---------------------------------------------------------------------------
_langfuse_client: Langfuse = None


def get_langfuse() -> Langfuse:
    """
    Get or create the Langfuse client singleton.

    Reads credentials from environment variables (loaded by python-dotenv).
    If keys aren't set, returns None — tracing is optional, the app still works.

    Think of this like a "lazy" login: we only connect to Langfuse the first
    time someone asks for the client, then reuse that same connection forever.
    """
    global _langfuse_client

    # Already created? Return it immediately (this is the "singleton" part)
    if _langfuse_client is not None:
        return _langfuse_client

    # Read keys from environment
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    host = os.environ.get("LANGFUSE_BASE_URL", os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"))

    # No keys? That's fine — tracing is optional
    if not secret_key or not public_key:
        logger.warning(
            "Langfuse keys not set (LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY). "
            "Tracing disabled — LLM calls will still work, just not logged."
        )
        return None

    # Create the client and store it for reuse
    _langfuse_client = Langfuse(
        secret_key=secret_key,
        public_key=public_key,
        host=host,
    )
    logger.info(f"Langfuse tracing enabled (host={host})")
    return _langfuse_client


def create_trace(
    name: str, 
    input_data: dict = None, 
    metadata: dict = None,
    user_id: str = None,
    session_id: str = None,
):
    """
    Create a new trace (top-level grouping) in Langfuse.

    A "trace" groups related LLM calls together. For example:
    - "search_similar_companies" trace contains the Tavily search + extraction calls
    - "score_company" trace contains the 4 scoring dimension calls

    Args:
        name: Name of the trace (e.g., "source_enrichment")
        input_data: Input data to log
        metadata: Additional metadata
        user_id: User identifier for tracking (e.g., email or anonymous ID)
        session_id: Session identifier for grouping traces in a session

    Returns the trace object, or None if tracing is disabled.
    Uses lf.trace() to create a proper trace that can receive scores.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        # Try multiple Langfuse API methods for compatibility across SDK versions
        trace = None
        trace_id = None
        
        # Method 1: SDK v3+ uses start_observation
        if hasattr(lf, 'start_observation'):
            trace = lf.start_observation(
                name=name,
                input=input_data,
                metadata=metadata,
            )
            trace_id = getattr(trace, 'id', None) or str(id(trace))
        # Method 2: SDK v2 uses trace()
        elif hasattr(lf, 'trace'):
            trace = lf.trace(
                name=name,
                input=input_data,
                metadata=metadata,
                user_id=user_id,
                session_id=session_id,
            )
            trace_id = getattr(trace, 'id', None)
        # Method 3: Fallback - just return a dummy wrapper
        else:
            logger.warning("Langfuse SDK doesn't have trace() or start_observation() - tracing disabled")
            return None
        
        logger.info(f"Created Langfuse trace: {trace_id} (user={user_id}, session={session_id})")
        
        # Return a wrapper with update/end methods for compatibility
        class TraceWrapper:
            def __init__(self, trace, trace_id):
                self.id = trace_id
                self._trace = trace
            
            def update(self, output=None, level=None, **kwargs):
                """Update the trace with output data."""
                try:
                    if output and hasattr(self._trace, 'update'):
                        self._trace.update(output=output)
                except Exception:
                    pass
            
            def end(self):
                """End the trace."""
                try:
                    if hasattr(self._trace, 'end'):
                        self._trace.end()
                except Exception:
                    pass
        
        return TraceWrapper(trace, trace_id)
    except Exception as e:
        # Tracing should never crash the app — log and continue
        logger.error(f"Failed to create trace: {e}")
        return None


def create_generation(name: str, model: str, input_data: str, metadata: dict = None):
    """
    Create a new generation (single LLM call) in Langfuse.

    A "generation" represents one prompt → response cycle with Gemini.
    Langfuse will show:
    - The exact prompt sent (input_data)
    - The model's response (added later via generation.update())
    - Token count, latency, cost

    We use as_type="generation" because this IS an LLM call.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        return lf.start_observation(
            name=name,
            as_type="generation",    # "generation" = one LLM call
            model=model,
            input=input_data,
            metadata=metadata,
        )
    except Exception as e:
        logger.debug(f"Failed to create generation: {e}")
        return None


def flush_langfuse():
    """
    Flush any pending Langfuse events before the process exits.

    Langfuse batches events for efficiency (sends them in bulk every few seconds).
    This ensures nothing is lost when the app shuts down or a Streamlit rerun happens.
    """
    if _langfuse_client is not None:
        _langfuse_client.flush()
        logger.info("Langfuse events flushed.")


# =============================================================================
# EVALUATION SCORES — Track grounding quality metrics
# =============================================================================

def score_trace(trace_id: str, name: str, value: float, comment: str = None):
    """
    Add a score to a trace in Langfuse.
    
    Args:
        trace_id: The trace ID to score
        name: Score name (e.g., "valid_website", "employee_count_found")
        value: Score value (0-1 for percentages, 0/1 for binary)
        comment: Optional explanation
    """
    lf = get_langfuse()
    if lf is None:
        logger.warning(f"Langfuse not initialized, skipping score: {name}")
        return
    
    if not trace_id:
        logger.warning(f"No trace_id provided, skipping score: {name}")
        return
    
    try:
        logger.info(f"Scoring trace {trace_id}: {name}={value}")
        # Use create_score (not score) - correct method name for this Langfuse version
        lf.create_score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment,
        )
        logger.info(f"Score sent successfully: {name}")
    except Exception as e:
        logger.error(f"Failed to score trace {trace_id}: {e}")


def evaluate_enrichment_batch(
    trace_id: str,
    enrichments: dict,
    search_results: list,
) -> dict:
    """
    Evaluate a batch of enriched companies and log scores to Langfuse.
    
    Metrics tracked:
    1. valid_website: % of companies with accessible website
    2. right_website: % of websites that match company name
    3. employee_count_rate: % of companies with found employee count
    4. stage_found_rate: % of companies with found funding stage
    5. location_mena_rate: % of companies in MENA region
    
    Args:
        trace_id: Langfuse trace ID for this enrichment run
        enrichments: Dict of company_name -> CompanyEnrichment
        search_results: List of SearchResult objects
    
    Returns:
        Dict with all computed metrics
    """
    from source_enrichment import MENA_COUNTRIES
    
    if not enrichments:
        return {}
    
    total = len(enrichments)
    
    # Counters
    valid_website_count = 0
    right_website_count = 0
    employee_count_found = 0
    stage_found = 0
    mena_location = 0
    
    for company_name, enrichment in enrichments.items():
        # 1. Valid website: Does the website URL exist and respond?
        if enrichment.website_url and enrichment.website_url.value:
            # Check if we successfully fetched content
            if enrichment.website_content:
                valid_website_count += 1
                
                # 2. Right website: Does the page contain the company name?
                company_lower = company_name.lower()
                content_lower = enrichment.website_content.lower()
                if company_lower in content_lower:
                    right_website_count += 1
        
        # 3. Employee count found
        if enrichment.employee_count and enrichment.employee_count.value:
            employee_count_found += 1
        
        # 4. Funding stage found
        if enrichment.funding_stage and enrichment.funding_stage.value:
            stage_found += 1
        
        # 5. Location in MENA
        if enrichment.location and enrichment.location.value:
            loc_lower = enrichment.location.value.lower()
            if any(country in loc_lower for country in MENA_COUNTRIES):
                mena_location += 1
    
    # Calculate rates
    metrics = {
        "valid_website": valid_website_count / total if total > 0 else 0,
        "right_website": right_website_count / total if total > 0 else 0,
        "employee_count_rate": employee_count_found / total if total > 0 else 0,
        "stage_found_rate": stage_found / total if total > 0 else 0,
        "location_mena_rate": mena_location / total if total > 0 else 0,
    }
    
    # Log to Langfuse
    if trace_id:
        for metric_name, metric_value in metrics.items():
            score_trace(
                trace_id=trace_id,
                name=metric_name,
                value=metric_value,
                comment=f"{metric_name}: {metric_value:.1%} ({int(metric_value * total)}/{total})"
            )
    
    logger.info(f"Enrichment metrics: {metrics}")
    return metrics


def evaluate_with_llm_judge(
    trace_id: str,
    company_name: str,
    enrichment_data: dict,
    score_data: dict,
) -> float:
    """
    Use LLM as a judge to evaluate the quality of enrichment and scoring.
    
    The LLM reviews:
    - Is the website correct for this company?
    - Is the employee count plausible?
    - Is the funding stage accurate?
    - Are the scores well-justified by evidence?
    
    Args:
        trace_id: Langfuse trace ID
        company_name: Name of the company
        enrichment_data: Dict from CompanyEnrichment.to_dict()
        score_data: Dict of dimension scores
    
    Returns:
        Score from 0-1 (1 = high quality, 0 = low quality)
    """
    from llm_client import call_gemini, parse_json_response
    
    prompt = f"""You are a quality reviewer for a VC deal sourcing tool.

Review the following enrichment and scoring data for accuracy and quality.

Company: {company_name}

Enrichment Data:
{enrichment_data}

Scoring Data:
{score_data}

Evaluate on these criteria:
1. Website Accuracy: Is the website URL plausible for this company name?
2. Data Completeness: Are key fields (employees, location, stage) filled?
3. Evidence Quality: Are quotes and sources provided for claims?
4. Score Justification: Do the scores have supporting evidence?

Return JSON:
{{
    "overall_score": 0.0 to 1.0,
    "website_accuracy": 0.0 to 1.0,
    "data_completeness": 0.0 to 1.0,
    "evidence_quality": 0.0 to 1.0,
    "score_justification": 0.0 to 1.0,
    "issues": ["list of any issues found"],
    "summary": "one sentence summary"
}}"""

    try:
        response = call_gemini(prompt, use_pro_model=False)
        data = parse_json_response(response)
        
        if not data:
            return 0.5  # Default if parsing fails
        
        overall = data.get("overall_score", 0.5)
        
        # Log to Langfuse
        if trace_id:
            score_trace(
                trace_id=trace_id,
                name="llm_judge_score",
                value=overall,
                comment=data.get("summary", "")
            )
            
            # Log sub-scores
            for sub_metric in ["website_accuracy", "data_completeness", 
                              "evidence_quality", "score_justification"]:
                if sub_metric in data:
                    score_trace(
                        trace_id=trace_id,
                        name=f"llm_judge_{sub_metric}",
                        value=data[sub_metric],
                    )
        
        return overall
        
    except Exception as e:
        logger.error(f"LLM judge evaluation failed: {e}")
        return 0.5
