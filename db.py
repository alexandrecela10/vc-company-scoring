"""
Database Client — single entry point for all Postgres interactions.

Uses psycopg2 directly to avoid Supabase SDK dependency conflicts.
Connection string is read from SUPABASE_DB_URL in .env.

To find your Supabase Postgres connection string:
  Supabase dashboard → Project Settings → Database → Connection string
  Use the "URI" format:
    postgresql://postgres:[PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres

Pattern: one get_conn() that opens a fresh connection per call (simple and
safe for Streamlit's multi-threaded re-run model), then one function per query.
"""

import os
import json
import logging
import psycopg2
import psycopg2.extras
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def get_conn():
    """
    Open a fresh psycopg2 connection using SUPABASE_DB_URL from .env.

    We open a new connection per call because Streamlit re-runs the
    entire script on each interaction — holding a global connection
    leads to 'connection closed' errors after idle periods.

    Returns a psycopg2 connection with autocommit=True so every
    INSERT/UPDATE takes effect immediately without an explicit commit.
    """
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise ValueError(
            "SUPABASE_DB_URL must be set in your .env file.\n"
            "Find it at: Supabase dashboard → Project Settings → Database → URI\n"
            "Format: postgresql://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres"
        )
    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _fetchall(sql: str, params=None) -> List[Dict]:
    """Run a SELECT and return all rows as a list of dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]


def _fetchone(sql: str, params=None) -> Optional[Dict]:
    """Run a SELECT and return one row as a dict, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def _execute(sql: str, params=None) -> Optional[Dict]:
    """Run an INSERT/UPDATE and return the first row (via RETURNING) or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            try:
                row = cur.fetchone()
                return dict(row) if row else None
            except psycopg2.ProgrammingError:
                return None


# Expose get_client as an alias so existing callers that call
# db.get_client().table(...) get a clear error pointing to the new pattern.
def get_client():
    raise NotImplementedError(
        "Supabase SDK removed. Use db helper functions directly (get_all_companies, etc.)"
    )


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def get_all_companies() -> List[Dict]:
    """Return all companies, ordered by name."""
    return _fetchall("SELECT * FROM company ORDER BY name")


def get_company(company_id: str) -> Optional[Dict]:
    """Return a single company by id."""
    return _fetchone("SELECT * FROM company WHERE id = %s", (company_id,))


def update_company_stage(company_id: str, stage: str) -> None:
    """Update the pipeline_stage on a company row."""
    _execute(
        "UPDATE company SET pipeline_stage = %s WHERE id = %s",
        (stage, company_id),
    )


# ---------------------------------------------------------------------------
# Metric Types
# ---------------------------------------------------------------------------

def get_metric_types() -> List[Dict]:
    """Return all metric types, ordered by name."""
    return _fetchall("SELECT * FROM metric_type ORDER BY name")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def get_metrics_for_type(metric_type_id: str) -> List[Dict]:
    """Return metrics belonging to a metric type.

    Only returns metrics that count for scoring (active + deprecated).
    Draft and archived metrics are hidden so scorecards stay consistent.
    """
    return _fetchall(
        """
        SELECT * FROM metric
        WHERE metric_type_id = %s
          AND lifecycle_state IN ('active','deprecated')
        """,
        (metric_type_id,),
    )


def get_all_metrics() -> List[Dict]:
    """
    Return all metrics with their parent metric_type fields nested under 'metric_type'.
    Mimics the Supabase select('*, metric_type(...)') join pattern.
    """
    # Only active + deprecated metrics count toward the scorecard.
    # Draft metrics are dry-run; archived metrics are soft-deleted.
    rows = _fetchall("""
        SELECT
            m.*,
            mt.name        AS mt_name,
            mt.must_have   AS mt_must_have,
            mt.house_weight AS mt_house_weight
        FROM metric m
        JOIN metric_type mt ON mt.id = m.metric_type_id
        WHERE m.lifecycle_state IN ('active','deprecated')
    """)
    # Nest metric_type fields into a sub-dict so scorer.py can do row['metric_type']['name']
    for r in rows:
        r["metric_type"] = {
            "name": r.pop("mt_name"),
            "must_have": r.pop("mt_must_have"),
            "house_weight": r.pop("mt_house_weight"),
        }
    return rows


# ---------------------------------------------------------------------------
# Company Metric Values — the core table
# ---------------------------------------------------------------------------

def get_latest_values_for_company(company_id: str) -> List[Dict]:
    """
    Return the latest CompanyMetricValue row per metric for a given company.
    Joins metric and data_source so the scorer has everything it needs in one call.
    """
    rows = _fetchall("""
        SELECT
            cmv.*,
            m.name          AS m_name,
            m.value_type    AS m_value_type,
            m.must_have     AS m_must_have,
            m.metric_type_id AS m_metric_type_id,
            m.obtain_method AS m_obtain_method,
            ds.name         AS ds_name
        FROM company_metric_value cmv
        JOIN metric m ON m.id = cmv.metric_id
        LEFT JOIN data_source ds ON ds.id = cmv.source_id
        WHERE cmv.company_id = %s AND cmv.is_latest = TRUE
    """, (company_id,))
    # Nest metric and data_source into sub-dicts to match the old Supabase shape
    for r in rows:
        r["metric"] = {
            "name": r.pop("m_name"),
            "value_type": r.pop("m_value_type"),
            "must_have": r.pop("m_must_have"),
            "metric_type_id": str(r.pop("m_metric_type_id")),
            "obtain_method": r.pop("m_obtain_method"),
        }
        r["data_source"] = {"name": r.pop("ds_name")}
    return rows


def get_value_history_for_metric(company_id: str, metric_id: str) -> List[Dict]:
    """Return full history of values for a company+metric pair, newest first."""
    rows = _fetchall("""
        SELECT cmv.*, ds.name AS ds_name
        FROM company_metric_value cmv
        LEFT JOIN data_source ds ON ds.id = cmv.source_id
        WHERE cmv.company_id = %s AND cmv.metric_id = %s
        ORDER BY cmv.captured_at DESC
    """, (company_id, metric_id))
    for r in rows:
        r["data_source"] = {"name": r.pop("ds_name")}
    return rows


def upsert_metric_value(
    company_id: str,
    metric_id: str,
    value: str,
    source_id: Optional[str],
    raw_evidence: str,
    captured_by: str,
    confidence: float = 1.0,
    override: bool = False,
    override_reason: Optional[str] = None,
    evidence_url: Optional[str] = None,
    url_verified: bool = False,
    url_verified_at: Optional[str] = None,
) -> Dict:
    """
    Insert a new CompanyMetricValue row as the latest value.

    Step 1: flip is_latest=False on all previous rows for this company+metric.
    Step 2: insert the new row with is_latest=True.

    `evidence_url` is the source URL (separate from the quote in raw_evidence).
    `url_verified` is set by link_verifier after fetching the URL and
    confirming the quote appears on the page.

    If override=True, agents will never overwrite this row.
    """
    # Step 1: demote previous latest rows
    _execute(
        "UPDATE company_metric_value SET is_latest = FALSE WHERE company_id = %s AND metric_id = %s",
        (company_id, metric_id),
    )
    # Step 2: insert new latest row
    return _execute("""
        INSERT INTO company_metric_value
            (company_id, metric_id, value, source_id, raw_evidence,
             captured_by, confidence, override, override_reason,
             evidence_url, url_verified, url_verified_at, is_latest)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
    """, (company_id, metric_id, value, source_id, raw_evidence,
          captured_by, confidence, override, override_reason,
          evidence_url, url_verified, url_verified_at)) or {}


def mark_url_verified(value_id: str, verified: bool) -> None:
    """Flip the url_verified flag on an existing metric value row."""
    _execute("""
        UPDATE company_metric_value
        SET url_verified = %s, url_verified_at = NOW()
        WHERE id = %s
    """, (verified, value_id))


# ---------------------------------------------------------------------------
# User Weights
# ---------------------------------------------------------------------------

def get_weights(user_id: str = "house") -> Dict[str, float]:
    """
    Return {metric_type_id: weight} for a given user.
    Personal weights are merged on top of house weights (personal wins).
    """
    house_rows = _fetchall(
        "SELECT metric_type_id, weight FROM user_weight WHERE user_id = 'house'"
    )
    house = {str(r["metric_type_id"]): float(r["weight"]) for r in house_rows}

    if user_id == "house":
        return house

    personal_rows = _fetchall(
        "SELECT metric_type_id, weight FROM user_weight WHERE user_id = %s",
        (user_id,),
    )
    personal = {str(r["metric_type_id"]): float(r["weight"]) for r in personal_rows}
    return {**house, **personal}  # personal overrides house


def upsert_weight(user_id: str, metric_type_id: str, weight: float) -> None:
    """Save or update a weight for a user + metric type."""
    _execute("""
        INSERT INTO user_weight (user_id, metric_type_id, weight)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, metric_type_id) DO UPDATE SET weight = EXCLUDED.weight, updated_at = NOW()
    """, (user_id, metric_type_id, weight))


# ---------------------------------------------------------------------------
# Pipeline Events
# ---------------------------------------------------------------------------

def get_pipeline_history(company_id: str) -> List[Dict]:
    """Return full pipeline stage history for a company, newest first."""
    return _fetchall(
        "SELECT * FROM pipeline_event WHERE company_id = %s ORDER BY changed_at DESC",
        (company_id,),
    )


def add_pipeline_event(
    company_id: str,
    from_stage: str,
    to_stage: str,
    changed_by: str,
    score_snapshot: Optional[float] = None,
    triggered_by: str = "manual",
) -> None:
    """Log a pipeline stage transition."""
    _execute("""
        INSERT INTO pipeline_event (company_id, from_stage, to_stage, changed_by, score_snapshot, triggered_by)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (company_id, from_stage, to_stage, changed_by, score_snapshot, triggered_by))


# ---------------------------------------------------------------------------
# Gap Actions
# ---------------------------------------------------------------------------

def get_gap_actions(company_id: str) -> List[Dict]:
    """
    Return the most-recent gap action for each (metric_id, gap_type) pair —
    so re-running the gap agent doesn't stack duplicate cards in the log.
    Older rows remain in the DB for audit; DISTINCT ON picks the newest.
    """
    rows = _fetchall("""
        SELECT DISTINCT ON (ga.metric_id, ga.gap_type)
            ga.*, m.name AS metric_name
        FROM gap_action ga
        JOIN metric m ON m.id = ga.metric_id
        WHERE ga.company_id = %s
        ORDER BY ga.metric_id, ga.gap_type, ga.created_at DESC
    """, (company_id,))
    # Re-sort the deduplicated set by created_at (newest first) for display
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    for r in rows:
        r["metric"] = {"name": r.pop("metric_name")}
    return rows


def log_gap_action(
    company_id: str,
    metric_id: str,
    gap_type: str,
    output_draft: str,
    status: str = "pending",
) -> Dict:
    """
    Log a gap action triggered by the gap agent.

    gap_type: 'outreach' (Type A — ask founder) or 'search' (Type B — web search)
    status:   'pending', 'sent', 'resolved'
    """
    return _execute("""
        INSERT INTO gap_action (company_id, metric_id, gap_type, output_draft, status)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (company_id, metric_id, gap_type, output_draft, status)) or {}


def update_gap_action_status(gap_action_id: str, status: str) -> None:
    """Update the status of a gap action (e.g., 'sent', 'resolved')."""
    _execute(
        "UPDATE gap_action SET status = %s WHERE id = %s",
        (status, gap_action_id),
    )


# ---------------------------------------------------------------------------
# Founders (company team members with LinkedIn profiles)
# ---------------------------------------------------------------------------

def get_founders(company_id: str) -> List[Dict]:
    """Return all founders for a company, ordered by creation."""
    return _fetchall(
        "SELECT * FROM founder WHERE company_id = %s ORDER BY created_at",
        (company_id,),
    )


# ---------------------------------------------------------------------------
# Discovery upserts — called by discovery.py after Alpha Scout finds a company
#
# Both are idempotent: re-running discovery with the same seed updates
# existing rows instead of creating duplicates. The UNIQUE indexes on
# LOWER(company.name) and (company_id, LOWER(founder.name)) enforce this.
# ---------------------------------------------------------------------------

def upsert_company_discovery(
    name: str,
    website: Optional[str],
    linkedin_url: Optional[str],
    country: Optional[str],
    industry: Optional[str],
    discovery_source_url: Optional[str],
    discovery_grounding_score: Optional[float],
    website_verified: bool = False,
    notes: Optional[str] = None,
) -> Dict:
    """
    Insert a company discovered by Alpha Scout, or update if it already exists.
    Dedup key is LOWER(name). Returns the company row (new or existing).

    We deliberately keep source_type='outbound' and source_channel='alpha_scout'
    so the UI clearly shows which companies came from discovery vs analyst inbound.
    """
    row = _execute("""
        INSERT INTO company (
            name, website, linkedin_url, country, industry,
            source_type, source_channel, notes,
            discovery_source_url, discovery_grounding_score,
            website_verified, discovered_at, pipeline_stage
        )
        VALUES (
            %s, %s, %s, %s, %s,
            'outbound', 'alpha_scout', %s,
            %s, %s,
            %s, NOW(), 'deal_sourcing'
        )
        ON CONFLICT (name) DO UPDATE SET
            website                   = COALESCE(EXCLUDED.website, company.website),
            linkedin_url              = COALESCE(EXCLUDED.linkedin_url, company.linkedin_url),
            country                   = COALESCE(EXCLUDED.country, company.country),
            industry                  = COALESCE(EXCLUDED.industry, company.industry),
            discovery_source_url      = COALESCE(EXCLUDED.discovery_source_url, company.discovery_source_url),
            discovery_grounding_score = COALESCE(EXCLUDED.discovery_grounding_score, company.discovery_grounding_score),
            website_verified          = company.website_verified OR EXCLUDED.website_verified,
            discovered_at             = COALESCE(company.discovered_at, EXCLUDED.discovered_at)
        RETURNING *
    """, (
        name, website, linkedin_url, country, industry,
        notes,
        discovery_source_url, discovery_grounding_score,
        website_verified,
    ))
    return row or {}


def upsert_founder_unverified(
    company_id: str,
    name: str,
    title: Optional[str] = None,
) -> Dict:
    """
    Insert a founder row with linkedin_verified=FALSE and no URL.
    The founder_linkedin.py resolver will later attempt verification and
    update the row via mark_founder_linkedin_verified(...).

    Dedup key: (company_id, LOWER(name)) so re-discovery doesn't duplicate.
    """
    row = _execute("""
        INSERT INTO founder (company_id, name, title, linkedin_verified)
        VALUES (%s, %s, %s, FALSE)
        ON CONFLICT (company_id, LOWER(name)) DO UPDATE SET
            title = COALESCE(EXCLUDED.title, founder.title)
        RETURNING *
    """, (company_id, name, title))
    return row or {}


def mark_founder_linkedin_verified(
    founder_id: str,
    linkedin_url: str,
    verified: bool,
    source_url: Optional[str] = None,
) -> None:
    """
    Update a founder row with their resolved LinkedIn.
    `verified=True` means the snippet at linkedin_url mentioned the company;
    `verified=False` means we found a candidate but couldn't confirm.
    UI must render a ⚠️ badge for unverified rows.
    """
    _execute("""
        UPDATE founder SET
            linkedin_url          = %s,
            linkedin_verified     = %s,
            linkedin_verified_at  = NOW(),
            linkedin_source_url   = COALESCE(%s, linkedin_source_url)
        WHERE id = %s
    """, (linkedin_url, verified, source_url, founder_id))


def get_or_create_data_source(name: str, source_type: str = "Platform") -> str:
    """Return the UUID of a data_source, creating it if missing."""
    existing = get_source_by_name(name)
    if existing:
        return str(existing["id"])
    row = _execute("""
        INSERT INTO data_source (name, source_type, can_automate, cost_tier, notes)
        VALUES (%s, %s, TRUE, 'low', 'Auto-created by discovery pipeline')
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (name, source_type))
    return str(row["id"]) if row else ""


def get_metric_by_id(metric_id: str) -> Optional[Dict]:
    """Return a single metric row by UUID — used when discovery seeds values by known metric id."""
    return _fetchone("SELECT * FROM metric WHERE id = %s", (metric_id,))


# ---------------------------------------------------------------------------
# Phase 1 — Provenance chain
# ---------------------------------------------------------------------------

def get_provenance_chain(company_id: str, metric_id: str) -> Optional[Dict]:
    """
    Walk the full provenance chain for a (company, metric) pair:
        company_metric_value
          → metric_observation  (winning_observation_id)
             → extraction_run   (extraction_run_id)
                → extractor     (extractor_id)
             → source_document  (optional)

    Returns a dict with the joined columns, or None if the row doesn't exist.
    When override=TRUE, `observation_*` fields will be NULL because the
    analyst's hand-set value is the winner, not an observation.

    Used by the UI provenance expander to answer "where did this value
    come from?" in one query.
    """
    return _fetchone("""
        SELECT
            cmv.value                  AS cmv_value,
            cmv.override               AS override,
            cmv.override_reason        AS override_reason,
            cmv.captured_at            AS cmv_captured_at,
            cmv.captured_by            AS cmv_captured_by,
            cmv.confidence             AS cmv_confidence,

            o.id                       AS observation_id,
            o.normalized_value         AS observation_value,
            o.raw_value                AS observation_raw_value,
            o.evidence_text            AS observation_evidence_text,
            o.evidence_url             AS observation_evidence_url,
            o.captured_at              AS observation_captured_at,
            o.confidence               AS observation_confidence,

            r.id                       AS extraction_run_id,
            r.started_at               AS run_started_at,
            r.status                   AS run_status,

            e.name                     AS extractor_name,
            e.version                  AS extractor_version,
            e.description              AS extractor_description,

            sd.id                      AS source_document_id,
            sd.source_type             AS source_type,
            sd.origin                  AS source_origin,
            sd.origin_path             AS source_origin_path,
            sd.origin_url              AS source_origin_url,
            sd.ingested_at             AS source_ingested_at
        FROM company_metric_value cmv
        LEFT JOIN metric_observation o ON o.id = cmv.winning_observation_id
        LEFT JOIN extraction_run    r ON r.id = o.extraction_run_id
        LEFT JOIN extractor         e ON e.id = r.extractor_id
        LEFT JOIN source_document   sd ON sd.id = o.source_document_id
        WHERE cmv.company_id = %s
          AND cmv.metric_id  = %s
          AND cmv.is_latest  = TRUE
    """, (company_id, metric_id))


# ---------------------------------------------------------------------------
# News cache — populated on-demand by company_intel.fetch_news()
# ---------------------------------------------------------------------------

def get_cached_news(company_id: str, limit: int = 10) -> List[Dict]:
    """Return the most recent cached news articles for a company."""
    return _fetchall("""
        SELECT * FROM news_article
        WHERE company_id = %s
        ORDER BY COALESCE(published_at, fetched_at) DESC
        LIMIT %s
    """, (company_id, limit))


def newest_news_fetch(company_id: str) -> Optional[Dict]:
    """Return the most recent fetched_at timestamp, or None if never fetched."""
    return _fetchone(
        "SELECT MAX(fetched_at) AS last_fetch FROM news_article WHERE company_id = %s",
        (company_id,),
    )


def upsert_news_article(
    company_id: str,
    title: str,
    url: str,
    snippet: Optional[str] = None,
    published_at: Optional[str] = None,
) -> None:
    """
    Insert a news article, or update fetched_at if the same URL already exists
    for this company. ON CONFLICT uses the unique (company_id, url) constraint.
    """
    _execute("""
        INSERT INTO news_article (company_id, title, url, snippet, published_at, fetched_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (company_id, url) DO UPDATE
        SET fetched_at = EXCLUDED.fetched_at,
            title      = EXCLUDED.title,
            snippet    = EXCLUDED.snippet
    """, (company_id, title, url, snippet, published_at))


# ---------------------------------------------------------------------------
# Pipeline stage transitions
# ---------------------------------------------------------------------------

def update_company_stage(
    company_id: str,
    new_stage: str,
    changed_by: str,
    score_snapshot: Optional[float] = None,
    triggered_by: str = "manual",
) -> Dict:
    """
    Atomically:
      1) read current stage
      2) update company.pipeline_stage
      3) insert a pipeline_event row capturing the transition + score snapshot
    Returns the pipeline_event row.
    """
    current = _fetchone(
        "SELECT pipeline_stage FROM company WHERE id = %s", (company_id,)
    )
    if not current:
        raise ValueError(f"Company {company_id} not found")
    from_stage = current["pipeline_stage"]

    _execute(
        "UPDATE company SET pipeline_stage = %s WHERE id = %s",
        (new_stage, company_id),
    )
    return _execute("""
        INSERT INTO pipeline_event
            (company_id, from_stage, to_stage, changed_by, triggered_by, score_snapshot)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (company_id, from_stage, new_stage, changed_by, triggered_by, score_snapshot)) or {}


# ---------------------------------------------------------------------------
# Data Sources
# ---------------------------------------------------------------------------

def get_all_data_sources() -> List[Dict]:
    """Return all known data sources."""
    return _fetchall("SELECT * FROM data_source ORDER BY name")


def get_source_by_name(name: str) -> Optional[Dict]:
    """Look up a data source by name (case-insensitive)."""
    return _fetchone(
        "SELECT * FROM data_source WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,)
    )


# ---------------------------------------------------------------------------
# Table Browser helper — raw table dump for the demo UI
# ---------------------------------------------------------------------------

def get_table_rows(table_name: str, limit: int = 200) -> List[Dict]:
    """
    Return raw rows from any table — used by the Table Browser tab in the UI.
    Only allows known table names to prevent SQL injection.
    """
    allowed = {
        "company", "data_source", "metric_type", "metric",
        "company_metric_value", "user_weight", "pipeline_event", "gap_action",
    }
    if table_name not in allowed:
        raise ValueError(f"Table '{table_name}' is not in the allowed list.")
    # Table name is safe — whitelisted above, so f-string is fine here
    return _fetchall(f"SELECT * FROM {table_name} LIMIT %s", (limit,))


# ---------------------------------------------------------------------------
# Phase 2a — extractor pipeline helpers
# ---------------------------------------------------------------------------
# Thin DB wrappers used by the extractor orchestrators (pipeline/extractors/*).
# Kept here rather than scattered across extractor modules so SQL lives in one
# place and we can swap Supabase for anything else later without touching the
# pipeline code.
# ---------------------------------------------------------------------------

def get_all_company_aliases() -> List[Dict]:
    """Return every company_alias row. Used by the entity resolver to build
    an in-memory lookup at startup. ~6-2000 rows in practice -- trivial cost."""
    return _fetchall(
        "SELECT id, company_id, alias, alias_type FROM company_alias"
    )


def insert_company_alias(
    company_id: str,
    alias: str,
    alias_type: str,
) -> Optional[Dict]:
    """Add an alias for a company. Idempotent on (company_id, alias, alias_type).

    alias_type must be one of 'legal_name','domain','dba','former_name','acronym'
    (enforced by CHECK constraint on the table).
    """
    return _execute(
        """
        INSERT INTO company_alias (company_id, alias, alias_type)
        VALUES (%s, %s, %s)
        ON CONFLICT (company_id, alias, alias_type) DO NOTHING
        RETURNING *
        """,
        (company_id, alias, alias_type),
    )


def get_extractor_by_name(name: str, version: str = "1.0") -> Optional[Dict]:
    """Return the extractor registry row by (name, version), or None."""
    return _fetchone(
        "SELECT * FROM extractor WHERE name = %s AND version = %s",
        (name, version),
    )


def get_metric_ids_by_code(codes: List[str]) -> Dict[str, str]:
    """Return {code: metric_id} for a list of metric.code values.

    Metrics with NULL code (legacy) or unknown codes are simply omitted
    from the result -- callers handle missing keys as "not wired up yet".
    """
    if not codes:
        return {}
    rows = _fetchall(
        "SELECT id, code FROM metric WHERE code = ANY(%s)",
        (codes,),
    )
    return {r["code"]: str(r["id"]) for r in rows}


def get_or_create_source_document(
    content_hash: str,
    source_type: str,
    source_subtype: Optional[str],
    origin: str,
    origin_path: Optional[str] = None,
    origin_url: Optional[str] = None,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    ingested_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict:
    """Idempotent upsert keyed by content_hash.

    Returns the row (new or existing) with a `_created` bool indicating
    whether we inserted a new row.
    """
    # ON CONFLICT DO UPDATE SET id = id returns the existing row; combined
    # with xmax=0 we can tell if the row was newly created on this call.
    row = _execute(
        """
        INSERT INTO source_document (
            content_hash, source_type, source_subtype, origin,
            origin_path, origin_url, mime_type, size_bytes, ingested_by, notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (content_hash) DO UPDATE SET content_hash = EXCLUDED.content_hash
        RETURNING *, (xmax = 0) AS _created
        """,
        (
            content_hash, source_type, source_subtype, origin,
            origin_path, origin_url, mime_type, size_bytes, ingested_by, notes,
        ),
    )
    return row or {}


def insert_source_chunks(
    source_document_id: str,
    chunks: List[Dict],
) -> Dict[str, str]:
    """Bulk-insert chunks; return {locator: chunk_id}.

    Each `chunks` item must have keys: text, locator, ordinal, page (optional).
    Uses ON CONFLICT on (source_document_id, chunk_idx) so re-running is a no-op.
    """
    if not chunks:
        return {}
    # execute_values is the psycopg2 idiom for bulk insert (one round-trip).
    # Build the tuple list explicitly so we can map locator -> chunk_idx.
    values = [
        (source_document_id, c["ordinal"], c.get("page"), c["text"])
        for c in chunks
    ]
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO source_chunk (source_document_id, chunk_idx, page, text)
            VALUES %s
            ON CONFLICT (source_document_id, chunk_idx) DO UPDATE SET text = EXCLUDED.text
            RETURNING id, chunk_idx
            """,
            values,
        )
        rows = cur.fetchall()

    # Map chunk_idx -> locator using the input ordinals; produce {locator: chunk_id}.
    idx_to_locator = {c["ordinal"]: c["locator"] for c in chunks}
    return {idx_to_locator[r["chunk_idx"]]: str(r["id"]) for r in rows}


def start_extraction_run(
    extractor_id: str,
    source_document_id: Optional[str],
    company_id: Optional[str],
) -> str:
    """Create an extraction_run row in `running` state, return its id."""
    row = _execute(
        """
        INSERT INTO extraction_run
            (extractor_id, source_document_id, company_id, status)
        VALUES (%s, %s, %s, 'running')
        RETURNING id
        """,
        (extractor_id, source_document_id, company_id),
    )
    return str(row["id"]) if row else ""


def finish_extraction_run(
    run_id: str,
    status: str,
    observations_count: int = 0,
    error_message: Optional[str] = None,
    cost_usd: Optional[float] = None,
) -> None:
    """Close an extraction_run: set finished_at, status, counts, cost."""
    _execute(
        """
        UPDATE extraction_run
           SET finished_at        = NOW(),
               status             = %s,
               observations_count = %s,
               error_message      = %s,
               cost_usd           = %s
         WHERE id = %s
        """,
        (status, observations_count, error_message, cost_usd, run_id),
    )


def insert_metric_observations(
    run_id: str,
    company_id: str,
    source_document_id: Optional[str],
    rows: List[Dict],
) -> int:
    """Bulk-insert metric_observation rows. Returns rows-inserted count.

    Each row dict must have: metric_id, normalized_value, raw_value (optional),
    evidence_text (optional), source_chunk_id (optional), confidence (optional).

    UNIQUE(extraction_run_id, company_id, metric_id) guarantees idempotency --
    re-running the same extractor on the same inputs is a no-op.
    """
    if not rows:
        return 0
    values = [
        (
            run_id,
            company_id,
            r["metric_id"],
            r.get("raw_value"),
            r["normalized_value"],
            source_document_id,
            r.get("source_chunk_id"),
            r.get("evidence_text"),
            r.get("confidence", 1.0),
        )
        for r in rows
    ]
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO metric_observation (
                extraction_run_id, company_id, metric_id,
                raw_value, normalized_value,
                source_document_id, source_chunk_id,
                evidence_text, confidence
            )
            VALUES %s
            ON CONFLICT (extraction_run_id, company_id, metric_id) DO NOTHING
            RETURNING id
            """,
            values,
        )
        return len(cur.fetchall())
