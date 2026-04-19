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
-- Uses a normalized lowercase name so "NovaPay" and "novapay" don't collide.
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
