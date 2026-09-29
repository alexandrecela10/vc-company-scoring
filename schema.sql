-- ============================================================
-- Company Scorer — Postgres Schema
-- Run this once in the Supabase SQL Editor (supabase.com)
-- ============================================================
-- Table creation order matters: referenced tables must exist
-- before foreign keys can point to them.
-- Order: data_source → metric_type → metric → company →
--        company_metric_value → user_weight →
--        pipeline_event → gap_action
-- ============================================================

-- Enable UUID generation (built into Supabase by default)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ============================================================
-- 1. DATA SOURCE
-- Known sources of company information.
-- e.g. Crunchbase, LinkedIn, Founder Interview, Tavily
-- ============================================================
CREATE TABLE IF NOT EXISTS data_source (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,           -- e.g. "Crunchbase"
    source_type TEXT NOT NULL,                  -- "API" | "Manual" | "File" | "Platform"
    can_automate BOOLEAN DEFAULT FALSE,         -- Can this source be scraped automatically?
    cost_tier   TEXT DEFAULT 'free',            -- "free" | "low" | "medium" | "high"
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 2. METRIC TYPE
-- Top-level scoring dimensions (e.g. "Founders Strength").
-- Each has a must_have flag and a house weight for the formula.
-- ============================================================
CREATE TABLE IF NOT EXISTS metric_type (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          TEXT NOT NULL UNIQUE,         -- e.g. "Founders Strength"
    description   TEXT,
    must_have     BOOLEAN DEFAULT FALSE,        -- If TRUE: missing value = NULL overall score
    house_weight  FLOAT NOT NULL DEFAULT 1.0,  -- Institutional default weight (will be normalised)
    created_at    TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 3. METRIC
-- Individual measurable signals within a metric type.
-- e.g. "Has Patent" lives inside "Technology Moat".
-- ============================================================
CREATE TABLE IF NOT EXISTS metric (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    metric_type_id UUID NOT NULL REFERENCES metric_type(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,               -- e.g. "Has Patent"
    description    TEXT,
    value_type     TEXT NOT NULL DEFAULT 'boolean',
    -- "boolean" | "number" | "text" | "score_1_5"
    must_have      BOOLEAN DEFAULT FALSE,       -- Inherits from parent type but can override
    obtain_method  TEXT DEFAULT 'manual',
    -- "manual" | "Ask Founders" | "API" | "Web Scrape" | "LLM"
    weight         FLOAT NOT NULL DEFAULT 1.0, -- Weight within its parent metric type
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (metric_type_id, name)
);


-- ============================================================
-- 4. COMPANY
-- One row per company in the deal pipeline.
-- ============================================================
CREATE TABLE IF NOT EXISTS company (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name           TEXT NOT NULL UNIQUE,
    website        TEXT,
    country        TEXT,
    industry       TEXT,
    pipeline_stage TEXT NOT NULL DEFAULT 'deal_sourcing',
    -- "deal_sourcing" | "first_contact" | "due_diligence" | "ic_review" | "passed" | "invested"
    source_type    TEXT DEFAULT 'inbound',     -- "inbound" | "outbound"
    source_channel TEXT,                       -- e.g. "Email", "Dropbox", "LinkedIn"
    notes          TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 5. COMPANY METRIC VALUE  ← THE CORE TABLE
-- Stores every measured value for every metric for every company.
-- Each row is one observation at one point in time.
--
-- is_latest=TRUE marks the current value — always query with
-- this filter for the live scorecard view.
--
-- override=TRUE locks the row — no agent may overwrite it.
-- ============================================================
-- NOTE: evidence_url / url_verified / url_verified_at added in migration
-- (see bottom of schema.sql). Keep the CREATE TABLE minimal so fresh runs
-- match the canonical shape via the ALTER statements below.
CREATE TABLE IF NOT EXISTS company_metric_value (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    metric_id       UUID NOT NULL REFERENCES metric(id) ON DELETE CASCADE,
    value           TEXT,                       -- Stored as text; cast in app layer
    source_id       UUID REFERENCES data_source(id),
    raw_evidence    TEXT,                       -- Exact quote / URL / file path proving this value
    confidence      FLOAT DEFAULT 1.0,          -- 0.0–1.0: how certain is this value?
    captured_at     TIMESTAMPTZ DEFAULT NOW(),
    captured_by     TEXT DEFAULT 'manual',      -- Analyst name | "market_agent" | "gap_agent"
    is_latest       BOOLEAN DEFAULT TRUE,       -- TRUE = current value for this company+metric
    override        BOOLEAN DEFAULT FALSE,      -- TRUE = analyst-locked, agents cannot overwrite
    override_reason TEXT                        -- Why this was manually locked
);

-- Index: fast lookup of current values for a company (used on every scorecard load)
CREATE INDEX IF NOT EXISTS idx_cmv_company_latest
    ON company_metric_value (company_id, is_latest);

-- Index: fast history lookup for a single metric
CREATE INDEX IF NOT EXISTS idx_cmv_metric_history
    ON company_metric_value (company_id, metric_id, captured_at DESC);

-- Invariant: at most ONE is_latest=TRUE row per (company, metric).
-- Unique partial index enforces this so duplicate seeds / double inserts are blocked.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cmv_one_latest_per_metric
    ON company_metric_value (company_id, metric_id)
    WHERE is_latest = TRUE;

-- Migration: split evidence into a URL column and add verification tracking.
--   evidence_url    — source link for the quote in raw_evidence
--   url_verified    — TRUE iff the URL is reachable AND the evidence quote
--                     appears verbatim in the fetched page (see link_verifier.py)
--   url_verified_at — when verification was last attempted
ALTER TABLE company_metric_value
    ADD COLUMN IF NOT EXISTS evidence_url    TEXT,
    ADD COLUMN IF NOT EXISTS url_verified    BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS url_verified_at TIMESTAMPTZ;

-- Migration: company intel (LinkedIn + founders + cached news).
-- Lets analysts dive deeper from the scorecard without leaving the tool.
ALTER TABLE company ADD COLUMN IF NOT EXISTS linkedin_url TEXT;

-- One company → many founders. Each founder has their own LinkedIn so we can
-- display the decision-makers directly on the scorecard.
CREATE TABLE IF NOT EXISTS founder (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id   UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    title        TEXT,                   -- CEO, CTO, etc.
    linkedin_url TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_founder_company ON founder (company_id);

-- News articles cache. Filled on-demand by company_intel.fetch_news()
-- via Tavily. UNIQUE(company_id, url) prevents duplicates on refresh.
CREATE TABLE IF NOT EXISTS news_article (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id    UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    snippet       TEXT,
    published_at  TIMESTAMPTZ,
    fetched_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (company_id, url)
);
CREATE INDEX IF NOT EXISTS idx_news_company_fetched
    ON news_article (company_id, fetched_at DESC);


-- ============================================================
-- Migration: DISCOVERY PROVENANCE
-- Every company either came from an analyst (inbound/outbound) OR from
-- the Alpha Scout discovery pipeline. These columns let us:
--   - trace each company back to the source URL we found it at
--   - show a grounding score in the UI (how trustworthy is this discovery?)
--   - flag when the website has been HTTP-verified to actually exist
-- All new columns are nullable so existing seeded rows stay valid.
-- ============================================================
ALTER TABLE company
    ADD COLUMN IF NOT EXISTS discovery_source_url       TEXT,
    ADD COLUMN IF NOT EXISTS discovery_grounding_score  FLOAT,
    ADD COLUMN IF NOT EXISTS website_verified           BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS discovered_at              TIMESTAMPTZ;

-- Dedup index for discovery re-runs.
-- Uses a normalized lowercase name so "Fintech A" and "fintech-a" don't collide.
-- Partial index (WHERE name IS NOT NULL) keeps it small and safe on NULLs.
CREATE UNIQUE INDEX IF NOT EXISTS uq_company_name_lower
    ON company (LOWER(name))
    WHERE name IS NOT NULL;


-- ============================================================
-- Migration: FOUNDER LINKEDIN VERIFICATION
-- Founder LinkedIn URLs are high-risk for hallucination (wrong person
-- with same name). We ALWAYS store whether the URL was verified:
--   verified=TRUE  → snippet at that URL mentions the company
--   verified=FALSE → URL stored but UI shows ⚠️ "unverified"
-- We never silently guess — the UI makes uncertainty visible.
-- ============================================================
ALTER TABLE founder
    ADD COLUMN IF NOT EXISTS linkedin_verified      BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS linkedin_verified_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS linkedin_source_url    TEXT;

-- Prevent duplicate founder rows on re-discovery of the same company.
-- (company_id, lower(name)) is the natural key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_founder_company_name_lower
    ON founder (company_id, LOWER(name));


-- ============================================================
-- 6. USER WEIGHT
-- Per-analyst weights for each metric type.
-- user_id = 'house' is the institutional default used by the
-- scoring formula when no personal weight is set.
-- ============================================================
CREATE TABLE IF NOT EXISTS user_weight (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id        TEXT NOT NULL,               -- Analyst email or "house"
    metric_type_id UUID NOT NULL REFERENCES metric_type(id) ON DELETE CASCADE,
    weight         FLOAT NOT NULL DEFAULT 1.0,
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, metric_type_id)            -- One weight per user per metric type
);


-- ============================================================
-- 7. PIPELINE EVENT
-- Immutable log of every stage transition for a company.
-- Stores a score snapshot so we can see "score when moved to IC".
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_event (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id     UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    from_stage     TEXT NOT NULL,
    to_stage       TEXT NOT NULL,
    changed_by     TEXT NOT NULL,               -- Analyst name or agent name
    triggered_by   TEXT DEFAULT 'manual',       -- "manual" | "score_threshold" | "agent"
    score_snapshot FLOAT,                       -- Overall score at the moment of transition
    changed_at     TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- 8. GAP ACTION
-- Logged when the gap agent detects a missing must-have metric.
-- Tracks what action was taken and its current status.
-- ============================================================
CREATE TABLE IF NOT EXISTS gap_action (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id    UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    metric_id     UUID NOT NULL REFERENCES metric(id) ON DELETE CASCADE,
    gap_type      TEXT NOT NULL,
    -- "outreach"  = Type A: draft email to founder (obtain_method = 'Ask Founders')
    -- "search"    = Type B: Tavily web search to find the data
    output_draft  TEXT,                         -- The generated email draft or search result
    status        TEXT NOT NULL DEFAULT 'pending',
    -- "pending" | "sent" | "resolved" | "failed"
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);


-- ============================================================
-- SCORING VIEW
-- Computes the current score for every company using Option B:
--
--   metric_type_score  = weighted_avg(latest metric values)
--   overall_score      = weighted_avg(type_scores)
--                        × PRODUCT(must_have_score / 5.0)
--
-- If any must_have metric type has NO value → overall = NULL.
--
-- This view is queried by scorer.py for the live scorecard.
-- It always uses is_latest=TRUE rows only.
-- ============================================================
CREATE OR REPLACE VIEW company_score_view AS
WITH

-- Step 1: Get latest numeric value for every company+metric pair
latest_values AS (
    SELECT
        cmv.company_id,
        cmv.metric_id,
        m.metric_type_id,
        m.weight         AS metric_weight,
        m.must_have      AS metric_must_have,
        mt.must_have     AS type_must_have,
        mt.house_weight  AS type_weight,
        -- Cast value to float; NULL if not a valid number
        CASE
            WHEN cmv.value ~ '^[0-9]+(\.[0-9]+)?$' THEN cmv.value::FLOAT
            WHEN cmv.value = 'true'  THEN 1.0
            WHEN cmv.value = 'false' THEN 0.0
            -- score_1_5 values stored as "1"–"5" are already numeric
            ELSE NULL
        END AS numeric_value
    FROM company_metric_value cmv
    JOIN metric m    ON m.id = cmv.metric_id
    JOIN metric_type mt ON mt.id = m.metric_type_id
    WHERE cmv.is_latest = TRUE
),

-- Step 2: Weighted average score per metric type per company
type_scores AS (
    SELECT
        company_id,
        metric_type_id,
        type_must_have,
        type_weight,
        -- Weighted average of child metric scores (1–5 scale)
        SUM(numeric_value * metric_weight) / NULLIF(SUM(metric_weight), 0) AS type_score,
        -- Flag: does this type have at least one must-have metric with a value?
        BOOL_AND(
            CASE WHEN metric_must_have THEN numeric_value IS NOT NULL ELSE TRUE END
        ) AS has_required_values
    FROM latest_values
    GROUP BY company_id, metric_type_id, type_must_have, type_weight
),

-- Step 3: Check if ALL must-have types have a score
must_have_check AS (
    SELECT
        ts.company_id,
        -- TRUE only if every must_have type has a score
        BOOL_AND(
            CASE WHEN ts.type_must_have THEN ts.type_score IS NOT NULL ELSE TRUE END
        ) AS all_must_haves_present,
        -- Multiplicative penalty: product of (must_have_score / 5.0) for each must-have type
        EXP(
            SUM(
                CASE WHEN ts.type_must_have AND ts.type_score IS NOT NULL
                    THEN LN(GREATEST(ts.type_score, 0.01) / 5.0)
                    ELSE 0
                END
            )
        ) AS must_have_multiplier
    FROM type_scores ts
    GROUP BY ts.company_id
),

-- Step 4: Weighted average of all type scores
base_score AS (
    SELECT
        company_id,
        SUM(type_score * type_weight) / NULLIF(SUM(type_weight), 0) AS weighted_avg_score
    FROM type_scores
    GROUP BY company_id
)

-- Step 5: Final score = weighted avg × must-have multiplier
-- NULL if any must-have type is missing
SELECT
    c.id   AS company_id,
    c.name AS company_name,
    c.pipeline_stage,
    CASE
        WHEN mhc.all_must_haves_present
            THEN ROUND(
                (bs.weighted_avg_score * mhc.must_have_multiplier)::NUMERIC, 2
            )
        ELSE NULL
    END AS overall_score,
    mhc.all_must_haves_present,
    mhc.must_have_multiplier,
    bs.weighted_avg_score
FROM company c
LEFT JOIN must_have_check mhc ON mhc.company_id = c.id
LEFT JOIN base_score      bs  ON bs.company_id  = c.id;


-- ============================================================
-- PHASE 1 — DATA PLATFORM FOUNDATION (2026-04)
-- ============================================================
-- Five new tables that move the architecture from "one flat fact table"
-- to a full provenance chain:
--
--   source_document  →  extraction_run  →  metric_observation  →  company_metric_value
--                           ▲
--                       extractor (registry)
--
-- Also: source_chunk (for long docs, supports Phase 4 RAG).
-- Also: ALTER company_metric_value to point at the winning observation.
--
-- See ARCHITECTURE.md §5 for the full schema rationale.
-- All tables are additive and idempotent (IF NOT EXISTS) so the file can
-- safely be re-run against an existing Supabase database.
-- ============================================================


-- ------------------------------------------------------------
-- SOURCE_DOCUMENT — one row per ingested artifact (PDF, email,
-- meeting note, Notion page, etc). Bronze layer in the medallion.
-- Content-hashed so re-ingesting the same file is a no-op.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_document (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_hash       TEXT NOT NULL UNIQUE,       -- sha256 of raw bytes; dedup key
    source_type        TEXT NOT NULL,              -- pitchdeck|meeting_note|email|notion_page|crm_record|web_page|analyst_note|seed
    source_subtype     TEXT,                       -- pdf|markdown|eml|html|csv
    origin             TEXT NOT NULL,              -- dropbox|manual_upload|gmail|tavily|notion_api|seed
    origin_path        TEXT,                       -- /Dropbox/.../tabby_deck.pdf
    origin_url         TEXT,                       -- source URL for web/external docs
    mime_type          TEXT,
    size_bytes         BIGINT,
    ingested_at        TIMESTAMPTZ DEFAULT NOW(),
    ingested_by        TEXT,                       -- analyst email or bot name
    text_extracted_at  TIMESTAMPTZ,                -- when we pulled raw text
    text_chars         INT,                        -- length of extracted text
    notes              TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_document_type
    ON source_document (source_type, ingested_at DESC);

COMMENT ON TABLE source_document IS
    'One row per ingested source artifact (pitch deck, meeting note, email, Notion page, web page, or seed record). Bronze layer. Content-hashed for dedup.';

COMMENT ON COLUMN source_document.content_hash IS
    'SHA-256 of the raw bytes. Used as a dedup key so re-ingesting the same file is a no-op.';


-- ------------------------------------------------------------
-- SOURCE_CHUNK — long documents split for semantic retrieval.
-- The `embedding` column stays NULL until Phase 4 (pgvector + HNSW).
-- Keeping the column here now so the Phase 1 migration doesn't need
-- to re-ALTER later.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_chunk (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_document_id  UUID NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    chunk_idx           INT NOT NULL,               -- 0-indexed order within the doc
    page                INT,                        -- page number for PDFs
    text                TEXT NOT NULL,
    embedding           FLOAT[],                    -- placeholder; upgrade to vector() in Phase 4
    embedded_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_document_id, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_source_chunk_document
    ON source_chunk (source_document_id, chunk_idx);

COMMENT ON TABLE source_chunk IS
    'Long documents split into chunks (~500 tokens) for retrieval. `embedding` is FLOAT[] placeholder until Phase 4 migrates it to pgvector.';


-- ------------------------------------------------------------
-- EXTRACTOR — registry of extractors. One row per (name, version).
-- Every metric_observation must point at an extractor — this is how
-- we know "who produced this fact and with what code version".
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extractor (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                     TEXT NOT NULL,                -- pitchdeck_gemini_v1|alpha_scout_v1|analyst_override|seed_data_v1
    version                  TEXT NOT NULL,                -- semver
    supported_source_types   TEXT[] NOT NULL,              -- e.g. {pitchdeck}
    supported_metric_ids     UUID[],                       -- NULL = can produce any metric
    description              TEXT,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (name, version)
);

COMMENT ON TABLE extractor IS
    'Registry of extractors. Every metric_observation points at one. Versioned so re-running with a new extractor version preserves old observations.';


-- ------------------------------------------------------------
-- EXTRACTION_RUN — one row per execution of an extractor against
-- a source (or against "nothing" for synthetic runs like the Phase 1
-- backfill and analyst overrides).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_run (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    extractor_id         UUID NOT NULL REFERENCES extractor(id) ON DELETE RESTRICT,
    source_document_id   UUID REFERENCES source_document(id) ON DELETE SET NULL,
    company_id           UUID REFERENCES company(id) ON DELETE SET NULL,
    started_at           TIMESTAMPTZ DEFAULT NOW(),
    finished_at          TIMESTAMPTZ,
    status               TEXT NOT NULL DEFAULT 'success'
                            CHECK (status IN ('running','success','error','partial')),
    observations_count   INT DEFAULT 0,
    error_message        TEXT,
    cost_usd             FLOAT,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_extraction_run_company
    ON extraction_run (company_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_extraction_run_document
    ON extraction_run (source_document_id, started_at DESC);

COMMENT ON TABLE extraction_run IS
    'One row per execution of an extractor. source_document_id and company_id may be NULL for non-document runs (e.g. discovery, analyst overrides, backfills).';


-- ------------------------------------------------------------
-- METRIC_OBSERVATION — the append-only, immutable fact table.
-- Every claim about a (company, metric) is a row here. The canonical
-- "winning" value is picked by the value resolver and cached on
-- company_metric_value.winning_observation_id.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_observation (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    extraction_run_id    UUID NOT NULL REFERENCES extraction_run(id) ON DELETE CASCADE,
    company_id           UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    metric_id            UUID NOT NULL REFERENCES metric(id) ON DELETE CASCADE,
    raw_value            TEXT,                       -- "$250k MRR", "Series C", "has 2 prior exits"
    normalized_value     TEXT NOT NULL,              -- "250000", "5", "true" — matches metric.value_type
    source_document_id   UUID REFERENCES source_document(id) ON DELETE SET NULL,
    source_chunk_id      UUID REFERENCES source_chunk(id) ON DELETE SET NULL,
    evidence_text        TEXT,                       -- literal quote supporting the claim
    evidence_url         TEXT,                       -- for external/web sources
    confidence           FLOAT DEFAULT 1.0,          -- 0.0-1.0
    as_of_date           DATE,                       -- date this claim refers to
    period_granularity   TEXT,                       -- point_in_time | month | quarter | year | trailing_12m
    scenario             TEXT,                       -- actual | estimate | projection | forecast
    currency             TEXT,                       -- ISO-4217 code when metric is monetary
    period_label         TEXT,                       -- raw source label, e.g. 2026E
    observation_fingerprint TEXT NOT NULL,           -- idempotency key for grounded fact
    captured_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at           TIMESTAMPTZ DEFAULT NOW(),

    -- Fingerprint uniqueness preserves time-series rows while making reruns
    -- idempotent for already-seen grounded facts.
    UNIQUE (observation_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_metric_observation_company_metric
    ON metric_observation (company_id, metric_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_observation_document
    ON metric_observation (source_document_id);

COMMENT ON TABLE metric_observation IS
    'Immutable, append-only fact table. Every claim about (company, metric) is a row here. company_metric_value picks one as the canonical "winning" value via the resolver.';


-- ------------------------------------------------------------
-- REFACTOR company_metric_value — add pointers to the winning
-- observation and (future) context fact. Keep override/override_reason
-- on this table because analyst overrides are the ONE case where the
-- canonical value is not derived from any observation.
-- ------------------------------------------------------------
ALTER TABLE company_metric_value
    ADD COLUMN IF NOT EXISTS winning_observation_id         UUID REFERENCES metric_observation(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS derived_from_context_fact_id   UUID;  -- FK target added in Phase 3

COMMENT ON COLUMN company_metric_value.winning_observation_id IS
    'The metric_observation row chosen as the canonical value by the resolver. NULL when override=TRUE or when the row was seeded pre-Phase-1.';

COMMENT ON COLUMN company_metric_value.derived_from_context_fact_id IS
    'Set when the value comes from a dimensional context_fact (e.g. industry CAGR). Populated in Phase 3.';


-- =====================================================================
-- Phase 2a migration: metric lifecycle states
-- =====================================================================
-- Why: we need to pause or retire a metric without deleting history.
--
-- States:
--   draft       -> extractors may run, but metric does NOT affect scores
--                  (safe A/B before committing to the scoring formula)
--   active      -> normal: extracted, scored, shown in UI
--   deprecated  -> no new extraction, existing values still score
--                  (graceful sunset so complete companies don't suddenly
--                   become "incomplete" when a must-have is retired)
--   archived    -> excluded everywhere; historical observations preserved
--
-- Default is 'active' so all 18 existing metrics keep their current behaviour.
-- =====================================================================
ALTER TABLE metric
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT
        NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('draft','active','deprecated','archived')),
    ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;

COMMENT ON COLUMN metric.lifecycle_state IS
    'draft=dry-run (no scoring), active=normal, deprecated=no new extraction but still scores, archived=excluded everywhere';
COMMENT ON COLUMN metric.deactivated_at IS
    'Timestamp when metric left the active state. Set manually when flipping to deprecated/archived.';

-- Rebuild company_score_view so draft + archived metrics drop out of scoring.
-- Only latest_values CTE changes (one extra WHERE clause); everything else identical.
-- (Phase 2a view rebuild — also appended a code column migration after this block.)
CREATE OR REPLACE VIEW company_score_view AS
WITH
latest_values AS (
    SELECT
        cmv.company_id,
        cmv.metric_id,
        m.metric_type_id,
        m.weight         AS metric_weight,
        m.must_have      AS metric_must_have,
        mt.must_have     AS type_must_have,
        mt.house_weight  AS type_weight,
        CASE
            WHEN cmv.value ~ '^[0-9]+(\.[0-9]+)?$' THEN cmv.value::FLOAT
            WHEN cmv.value = 'true'  THEN 1.0
            WHEN cmv.value = 'false' THEN 0.0
            ELSE NULL
        END AS numeric_value
    FROM company_metric_value cmv
    JOIN metric m    ON m.id = cmv.metric_id
    JOIN metric_type mt ON mt.id = m.metric_type_id
    WHERE cmv.is_latest = TRUE
      AND m.lifecycle_state IN ('active','deprecated')   -- NEW: exclude draft + archived
),
type_scores AS (
    SELECT
        company_id,
        metric_type_id,
        type_must_have,
        type_weight,
        SUM(numeric_value * metric_weight) / NULLIF(SUM(metric_weight), 0) AS type_score,
        BOOL_AND(
            CASE WHEN metric_must_have THEN numeric_value IS NOT NULL ELSE TRUE END
        ) AS has_required_values
    FROM latest_values
    GROUP BY company_id, metric_type_id, type_must_have, type_weight
),
must_have_check AS (
    SELECT
        ts.company_id,
        BOOL_AND(
            CASE WHEN ts.type_must_have THEN ts.type_score IS NOT NULL ELSE TRUE END
        ) AS all_must_haves_present,
        EXP(
            SUM(
                CASE WHEN ts.type_must_have AND ts.type_score IS NOT NULL
                    THEN LN(GREATEST(ts.type_score, 0.01) / 5.0)
                    ELSE 0
                END
            )
        ) AS must_have_multiplier
    FROM type_scores ts
    GROUP BY ts.company_id
),
base_score AS (
    SELECT
        company_id,
        SUM(type_score * type_weight) / NULLIF(SUM(type_weight), 0) AS weighted_avg_score
    FROM type_scores
    GROUP BY company_id
)
SELECT
    c.id   AS company_id,
    c.name AS company_name,
    c.pipeline_stage,
    CASE
        WHEN mhc.all_must_haves_present
            THEN ROUND(
                (bs.weighted_avg_score * mhc.must_have_multiplier)::NUMERIC, 2
            )
        ELSE NULL
    END AS overall_score,
    mhc.all_must_haves_present,
    mhc.must_have_multiplier,
    bs.weighted_avg_score
FROM company c
LEFT JOIN must_have_check mhc ON mhc.company_id = c.id
LEFT JOIN base_score      bs  ON bs.company_id  = c.id;


-- =====================================================================
-- Phase 2a migration: metric.code (stable snake_case identifier)
-- =====================================================================
-- Why: rules.yaml, gap_action codes, and future analytics filters need a
-- stable reference to each metric. metric.name is human-readable and
-- editable; metric.id is a UUID (unreadable in YAML). metric.code fills
-- the gap.
--
-- Contract:
--   * snake_case, starts with a lowercase letter
--   * UNIQUE (one code per metric, one metric per code)
--   * Nullable -- legacy metrics without extraction rules can stay NULL
--   * Immutable by convention (renaming a code breaks rules.yaml)
-- =====================================================================
ALTER TABLE metric
    ADD COLUMN IF NOT EXISTS code TEXT UNIQUE
        CHECK (code IS NULL OR code ~ '^[a-z][a-z0-9_]*$');

COMMENT ON COLUMN metric.code IS
    'Stable snake_case identifier used by rules.yaml and non-UI callers. '
    'Immutable by convention. NULL for legacy metrics without extraction rules.';


-- =====================================================================
-- Phase 2a migration: company_alias (entity resolution aliases)
-- =====================================================================
-- Why: when an analyst uploads a deck, the filename / first slide
-- often contains a legal name, a DBA, a former name, or a domain that
-- doesn't match company.name exactly. We need a place to store all
-- the "other ways this company is spelled" so the entity resolver
-- can match with high precision.
--
-- One company -> many aliases (one row per alias_type variant).
-- alias_type enumerates the provenance of the alias so the UI can
-- show "legal name", "domain", etc. without string-sniffing.
-- =====================================================================
CREATE TABLE IF NOT EXISTS company_alias (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id  UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_type  TEXT NOT NULL
        CHECK (alias_type IN ('legal_name','domain','dba','former_name','acronym')),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    -- Same alias can exist once per (company, alias_type). Two different
    -- companies can legitimately share an alias (e.g. "Rize" DBA) - we
    -- let the resolver disambiguate by signals.
    UNIQUE (company_id, alias, alias_type)
);

-- Functional index on lowercased alias. The resolver does all lookups
-- case-insensitively, so this makes `WHERE LOWER(alias) = $1` fast.
CREATE INDEX IF NOT EXISTS idx_company_alias_lower
    ON company_alias (LOWER(alias));

-- Secondary index to quickly fetch all aliases for a given company
-- (used on the company detail page in the UI).
CREATE INDEX IF NOT EXISTS idx_company_alias_company
    ON company_alias (company_id);

COMMENT ON TABLE company_alias IS
    'Append-only aliases for a company (legal name, domain, DBA, former name, acronym). '
    'Read by the entity resolver to match uploaded documents to the correct company_id.';
