# Company Scorer — Data Architecture

> **Status**: Draft · **Owner**: Alexandre Cela · **Last updated**: 2026-04-19

This document is the source of truth for the data platform powering the
Company Scorer. It describes the target architecture, the schemas, the
ingestion and extraction flows, and the phased roadmap to get there from
today's state.

If you are reading this and the code disagrees, the code is behind. Open
a PR to update this file alongside any schema change.

---

## 1. Goals

1. **Grounded by default**. Every metric value in the scorer is linked
   back to a specific document (or a dated external signal), not to a
   free-floating LLM call.
2. **Structured-first, retrieval second**. Pre-extract facts into typed
   columns at ingestion time so 80% of queries are pure SQL with zero
   retrieval and zero LLM calls at read time. See §6.5.
3. **Many source types, one pipeline**. Pitch decks, meeting notes,
   emails, Notion exports, CRM dumps, periodic web searches — all land
   in the same structured model.
4. **Detection-first**. The system identifies which company a source is
   about before extracting anything.
5. **Analyst always wins**. Any analyst override is sacred — no agent,
   extractor, or refresh job may silently overwrite it.
6. **LLM-queryable**. The schema is designed so an LLM agent can reliably
   generate correct SQL + charts from a natural-language analyst
   question, without hand-holding. See §6.6.
7. **Observable**. Every ingestion, extraction, and refresh leaves a
   trail: what ran, what it produced, what failed.

## 2. Non-goals (for now)

- Multi-tenancy / organization-scoped data. Single-user today.
- Real-time event streaming. Batch/polling is fine.
- Own vector store. We use Supabase `pgvector`.
- Workflow engines (Airflow / Dagster). `cron` + a `job_run` table is
  sufficient until we have 20+ recurring jobs.
- Full event sourcing / change data capture.

---

## 3. Current state (as of 2026-04-19)

### 3.1 Tables that exist

| Table | Role |
|---|---|
| `company` | One row per company. Includes discovery provenance (`discovery_source_url`, `grounding_score`, `website_verified`). |
| `founder` | One row per founder. LinkedIn verification flag. |
| `metric_type` | Scoring dimensions (Founders Strength, Market Growth, …). |
| `metric` | Measurable signals inside each dimension. |
| `company_metric_value` | **The only fact table today.** One row per latest value per (company, metric). Carries `raw_evidence`, `evidence_url`, `captured_by`. |
| `data_source` | Reference list of source *types* (Crunchbase, LinkedIn, Alpha Scout…). |
| `user_weight` | Per-user weight overrides on metric types. |
| `pipeline_event` | Immutable log of stage transitions. |
| `gap_action` | Outputs from the gap agent (emails, searches). |
| `news_article` | Cached news per company. |

### 3.2 What works well

- Scoring view (`company_score_view`) computes overall scores purely in
  Postgres. No Python round-trip on read.
- `is_latest` + `override` flags on `company_metric_value` give
  analysts a working override mechanism.
- Alpha Scout discovery pipeline writes grounded companies with
  attributable URLs.
- `linkedin_verified` on `founder` forces the UI to show uncertainty.

### 3.3 What's missing — the 10 gaps

| # | Gap | Consequence |
|---|---|---|
| 1 | No `source_document` layer. `data_source` stores TYPES, not files. | No way to re-process a doc, de-dupe an upload, or link a metric value to the exact sentence it came from. |
| 2 | Company detection is implicit. Only `discovery.py` creates companies. | Dropping a pitch deck, meeting note, email, or Notion page anywhere doesn't auto-link to a company. |
| 3 | Provenance chain stops at `evidence_url` (one string). | Can't audit `"Series C"` back to page 4 of `tabby_deck.pdf`. |
| 4 | Cross-company metrics (market growth by region×industry) are not modeled. | Forced to duplicate the same market fact on every company row, or re-call an LLM each view. |
| 5 | Observations and scored values are conflated in one TEXT field. | Raw "$250k MRR" is lost after normalization. No source agreement logic. |
| 6 | No semantic layer. `source_chunk` + embeddings don't exist. | Can't do RAG, semantic search, or fast similar-company lookups. |
| 7 | No scheduler + no `job_run` observability table. | "Refresh every 2 weeks" has no home. Silent failures would go unnoticed. |
| 8 | No uniform extractor interface / registry. | Each source type would reinvent its own output shape. No versioning of extractors. |
| 9 | No data quality / validation layer. | Type mismatches and stale data rot silently. |
| 10 | No multi-tenancy / RBAC. | Fine today; flagged for future. |

---

## 4. Target architecture — medallion pattern

Three layers. Each layer has a distinct purpose and contract.

```
┌───────────────────────────┐   ┌───────────────────────────┐   ┌───────────────────────────┐
│  BRONZE (raw artifacts)   │──▶│  SILVER (structured)      │──▶│  GOLD (analyst-ready)     │
│                           │   │                           │   │                           │
│  Dropbox data lake        │   │  source_document          │   │  company                  │
│   ├─ pitch decks (PDF)    │   │  source_chunk + embedding │   │  company_metric_value     │
│   ├─ meeting notes (md)   │   │  document_company_link    │   │   (current truth view)    │
│   ├─ emails (eml)         │   │  extractor / extraction_run│  │  context_fact             │
│   ├─ Notion exports       │   │  metric_observation       │   │   (market / region facts)│
│   └─ CRM exports (csv)    │   │  company_alias            │   │                           │
│                           │   │                           │   │                           │
│  External APIs            │   │                           │   │                           │
│   ├─ Tavily periodic      │   │                           │   │                           │
│   └─ Gemini search        │   │                           │   │                           │
└───────────────────────────┘   └───────────────────────────┘   └───────────────────────────┘
   Raw files & signals          Cleaned, linked, versioned       The scorecard reads from here
   (immutable once ingested)    (immutable observations)         (mutable canonical values)
```

**Contract between layers**:

- **Bronze → Silver**: every file is ingested into `source_document`
  with a content hash. Long docs are split into `source_chunk` rows.
  An entity resolver links each chunk to zero or more companies via
  `document_company_link`.
- **Silver → Gold**: extractors produce immutable `metric_observation`
  rows tied to an `extraction_run`. `company_metric_value` is derived
  (the "winning" observation per company+metric after resolving
  conflicts and analyst overrides).
- **External signals → Gold**: `context_fact` carries time-bound
  market/region/industry facts keyed by dimensions, not by company.

---

## 5. Target schema

Full DDL lives in `schema.sql`. Summaries below.

### 5.1 Source ingestion (new)

#### `source_document`
One row per ingested artifact (pitch deck, meeting note, email, CSV row,
Notion page). Content-hashed to detect duplicates.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `content_hash` | TEXT UNIQUE | SHA-256 of the raw bytes. Dedup key. |
| `source_type` | TEXT | `pitchdeck`, `meeting_note`, `email`, `notion_page`, `crm_record`, `web_page`, `analyst_note` |
| `source_subtype` | TEXT NULL | e.g. `pdf`, `markdown`, `eml` |
| `origin` | TEXT | `dropbox`, `manual_upload`, `gmail`, `tavily`, `notion_api` |
| `origin_path` | TEXT NULL | `/Dropbox/.../tabby_seriesB.pdf` |
| `origin_url` | TEXT NULL | Source URL for web / external docs |
| `mime_type` | TEXT NULL | |
| `size_bytes` | BIGINT NULL | |
| `ingested_at` | TIMESTAMPTZ | |
| `ingested_by` | TEXT | analyst email or `dropbox_watcher` |
| `text_extracted_at` | TIMESTAMPTZ NULL | When we pulled raw text from PDF/etc |
| `text_chars` | INT NULL | |
| `notes` | TEXT NULL | |

#### `source_chunk`
Long documents are split into chunks (~500 tokens). Each chunk carries
an embedding for semantic retrieval.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `source_document_id` | UUID FK | |
| `chunk_idx` | INT | 0-indexed order in the doc |
| `page` | INT NULL | For PDFs |
| `text` | TEXT | Chunk text |
| `embedding` | VECTOR(1536) NULL | `pgvector`; filled after ingestion |
| `embedded_at` | TIMESTAMPTZ NULL | |

Index: HNSW on `embedding` for fast similarity search.

#### `document_company_link`
A single document can be about multiple companies (e.g. a market report).
This is the entity-resolution output.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `source_document_id` | UUID FK | |
| `company_id` | UUID FK | |
| `confidence` | FLOAT | 0.0–1.0 |
| `detection_method` | TEXT | `exact_name`, `alias_match`, `domain_match`, `fuzzy_name`, `llm_inference` |
| `evidence` | TEXT NULL | What triggered the match |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(source_document_id, company_id)`.

### 5.2 Entity resolution (new)

#### `company_alias`
Alternate names, domains, and handles that all point to one canonical
company.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `company_id` | UUID FK | |
| `alias` | TEXT | Case-insensitive |
| `alias_type` | TEXT | `legal_name`, `brand`, `short_name`, `domain`, `email_domain`, `social_handle` |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(LOWER(alias), alias_type)`.

### 5.3 Extraction pipeline (new)

#### `extractor`
Registry of extractors. One row per (name, version).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | TEXT | `pitchdeck_gemini_v1`, `meeting_note_md_v1`, `alpha_scout_v1`, `analyst_override` |
| `version` | TEXT | Semver |
| `supported_source_types` | TEXT[] | e.g. `{pitchdeck}` |
| `supported_metric_ids` | UUID[] NULL | NULL = can produce any |
| `description` | TEXT NULL | |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(name, version)`.

#### `extraction_run`
One row per execution of an extractor against a document.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `extractor_id` | UUID FK | |
| `source_document_id` | UUID FK NULL | NULL for non-document extractors (e.g. Alpha Scout discovery) |
| `company_id` | UUID FK NULL | NULL if run is multi-company |
| `started_at` | TIMESTAMPTZ | |
| `finished_at` | TIMESTAMPTZ NULL | |
| `status` | TEXT | `running`, `success`, `error`, `partial` |
| `observations_count` | INT DEFAULT 0 | |
| `error_message` | TEXT NULL | |
| `cost_usd` | FLOAT NULL | LLM/Tavily cost, for observability |

#### `metric_observation`
**Immutable** observations. Replaces direct writes to
`company_metric_value`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `extraction_run_id` | UUID FK | |
| `company_id` | UUID FK | |
| `metric_id` | UUID FK | |
| `raw_value` | TEXT | "$250k MRR", "Series C", "has 2 prior exits" |
| `normalized_value` | TEXT | "250000", "5" (score), "true" — matches metric.value_type |
| `source_document_id` | UUID FK NULL | |
| `source_chunk_id` | UUID FK NULL | |
| `evidence_text` | TEXT NULL | The literal quote |
| `evidence_url` | TEXT NULL | For external/web sources |
| `confidence` | FLOAT DEFAULT 1.0 | 0.0–1.0 |
| `captured_at` | TIMESTAMPTZ | |

UNIQUE `(extraction_run_id, company_id, metric_id)` — one observation
per (run, company, metric).

### 5.4 Context metrics (new)

#### `context_fact`
Time-bound facts keyed by dimensions. Used for market/region/industry
signals that apply to many companies at once.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `dimensions` | JSONB | `{"region": "MENA", "industry": "Fintech"}` |
| `metric_name` | TEXT | `industry_cagr_3y`, `regional_vc_activity_score` |
| `value` | FLOAT | |
| `unit` | TEXT NULL | `percent`, `usd`, `score_1_5` |
| `source_url` | TEXT | Grounding URL |
| `source_name` | TEXT NULL | e.g. "McKinsey MENA Fintech Report 2026" |
| `captured_at` | TIMESTAMPTZ | |
| `valid_until` | TIMESTAMPTZ NULL | When we should refresh |
| `extraction_run_id` | UUID FK NULL | Links back to the refresh run |

Index: GIN on `dimensions`, B-tree on `metric_name`.

#### Mapping context → company metrics
Context facts are joined into company scores via a lightweight mapping:

```
company (country='Saudi Arabia', industry='Fintech')
    ↓ lookup
context_fact (dimensions={region:'MENA', industry:'Fintech'},
              metric_name='industry_cagr_3y', value=14.2)
    ↓ rule: CAGR > 10 → is_growing_industry=TRUE
company_metric_value (metric='Is in Growing Industry', value='true',
                      derived_from_context_fact_id=<uuid>)
```

This keeps `company_metric_value` as the single read path for the
scorer view, while market signals stay deduplicated.

### 5.5 Jobs & observability (new)

#### `scheduled_job`
Registry of periodic tasks.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | TEXT UNIQUE | `market_refresh`, `news_scan`, `founder_linkedin_sweep` |
| `schedule_cron` | TEXT | `"0 3 * * 1"` (Mondays 3am) |
| `command` | TEXT | `python market_refresh.py` |
| `enabled` | BOOLEAN | |
| `last_run_at` | TIMESTAMPTZ NULL | |
| `next_run_at` | TIMESTAMPTZ NULL | |

#### `job_run`
One row per execution — success or failure.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `job_id` | UUID FK | |
| `started_at` | TIMESTAMPTZ | |
| `finished_at` | TIMESTAMPTZ NULL | |
| `status` | TEXT | `running`, `success`, `error` |
| `stats` | JSONB | `{"companies_updated": 12, "facts_refreshed": 5}` |
| `error_message` | TEXT NULL | |
| `log_excerpt` | TEXT NULL | Last ~200 lines |

### 5.6 Refactor of existing tables

#### `company_metric_value` (keep, but change semantics)
Becomes the **canonical / winning value** per (company, metric). No
longer the primary write target.

New columns:

| Column | Type | Notes |
|---|---|---|
| `winning_observation_id` | UUID FK NULL | Points to `metric_observation` |
| `derived_from_context_fact_id` | UUID FK NULL | When value comes from a context fact |

Keep: `override`, `override_reason` (analyst-authored, not from any
observation).

**Write rules**:
- If `override = TRUE` → value is analyst-set; ignore observations.
- Else → computed from `metric_observation` rows via a resolver (source
  priority + recency + confidence).

#### `data_source` (keep, but clarify role)
Stays as a reference list of source TYPES (e.g. Crunchbase, LinkedIn).
NOT the same as `source_document` (individual artifacts).

---

## 6. Key flows

### 6.1 Ingesting a pitch deck from Dropbox

```
 1. Dropbox watcher sees /Dropbox/Companies/Tabby/seriesB_deck.pdf
 2. Hash file → check source_document for existing hash
      → if found: skip (idempotent)
 3. Insert source_document (origin='dropbox', source_type='pitchdeck')
 4. Entity resolve:
      a. Parse filename + first-page text for company signals
      b. Match against company.name + company_alias
      c. If confidence ≥ 0.7 → insert document_company_link
      d. If ambiguous → enqueue for analyst review
 5. Extract text with PDF parser (pdfplumber)
 6. Split into chunks (500 tokens, 50 overlap)
      → insert source_chunk rows
      → async: compute embeddings
 7. Trigger extraction_run with extractor='pitchdeck_gemini_v1'
      → Gemini prompt: "Extract metric observations from these chunks"
      → each observation: metric_id, raw_value, normalized_value,
        evidence_text, confidence, source_chunk_id
      → insert metric_observation rows
 8. Refresh company_metric_value for each affected (company, metric):
      → skip if override=TRUE
      → else pick winning observation per source priority + recency
 9. Bump company_version(company_id) so UI caches invalidate
```

### 6.2 Reading a scorecard

```
 1. UI calls get_latest_values_for_company(company_id)
 2. Postgres returns company_metric_value rows (already canonical)
 3. For each row, UI can drill down:
      a. If override=TRUE → show "analyst override"
      b. Elif winning_observation_id → join to metric_observation
         → source_document → source_chunk → show quote + doc link
      c. Elif derived_from_context_fact_id → show context fact card
         ("MENA Fintech CAGR 14%, refreshed 3d ago, source: McKinsey")
 4. No LLM call on read. All grounding pre-computed.
```

### 6.3 Refreshing market signals (biweekly)

```
 1. cron triggers market_refresh.py (or pg_cron calls a function)
 2. Start job_run (status=running)
 3. For each (region, industry) pair active in company table:
      a. Call Tavily search: "MENA Fintech CAGR 2026"
      b. Use Gemini to extract a single number + source URL
      c. Insert context_fact (dimensions, metric_name, value, source_url,
         valid_until = now() + 14 days)
 4. For each company matching a refreshed (region, industry):
      → update derived company_metric_value if rule changed
      → bump company_version
 5. Finish job_run (status=success, stats={facts_refreshed: N, …})
```

### 6.4 Analyst drops a meeting note

```
 1. Analyst uploads `tabby_meeting_2026-04-18.md` via UI or drops in Dropbox
 2. Same ingest flow as 6.1 (source_type='meeting_note')
 3. Extractor 'meeting_note_md_v1' runs Gemini:
      → extract metric observations + updated pipeline stage hints
 4. Observations inserted; company_metric_value updated
 5. Analyst sees the new values on the scorecard with 'source: meeting note'
```

### 6.5 Retrieval strategy — structured first, RAG as fallback

> **★ Core principle**: every metric we pre-extract into a typed column at
> **ingestion time** eliminates an LLM call at **query time**. The magic is
> not RAG — the magic is moving facts *out of documents* into the structured
> DB so retrieval is rarely needed.

Three tiers of retrieval, in order of preference:

```
┌──────────────────────────────────────────────────────────────────┐
│ Tier 1 — Structured SQL (~80% of queries)                        │
│  "Top 10 fintechs by score", "companies with stale MRR",         │
│  "which MENA companies raised Series B this quarter?"            │
│  → SQL over company, company_metric_value, context_fact, etc.    │
│  → No retrieval. No LLM at read time. Pure SQL.                  │
├──────────────────────────────────────────────────────────────────┤
│ Tier 2 — Hybrid text search (~15% of queries)                    │
│  "what did Tabby say about payback period in any doc?"           │
│  → BM25 (Postgres FTS)  +  pgvector (dense embeddings)           │
│  → Fused via Reciprocal Rank Fusion (RRF)                        │
│  → Optional cross-encoder reranker on top-20 → top-5             │
├──────────────────────────────────────────────────────────────────┤
│ Tier 3 — LLM reasoning (~5% of queries)                          │
│  "compare Tabby and Tamara's GTM based on their decks"           │
│  → Pull chunks via Tier 2, THEN ask Gemini to synthesize         │
│  → ONLY path where an LLM reads raw text at query time           │
└──────────────────────────────────────────────────────────────────┘
```

**Why hybrid (BM25 + vector), not one or the other:**

| Query example | Keyword-only | Semantic-only | Hybrid (winner) |
|---|---|---|---|
| `mentioned FedNow in pitch decks` | perfect (exact term) | degrades — also returns "real-time payments" | both exact + related |
| `founders with prior fintech exits` | misses "founded and sold", "acquired my company" | captures all phrasings | |
| `MRR > $200k` | N/A | N/A | **pure SQL Tier 1 — no retrieval** |

**Implementation**:

- **BM25**: Postgres native FTS via `tsvector` column on `source_chunk.text`,
  GIN index, `ts_rank_cd` for ranking.
- **Dense**: `pgvector` VECTOR(1536) embeddings, HNSW index,
  cosine distance.
- **Fusion**: RRF with `k=60` standard constant,
  `score = Σ 1/(k + rank_i)` — 10 lines of SQL/Python, no tuning.
- **Reranker** (Phase 4+): `BAAI/bge-reranker-v2-m3` or Cohere rerank API
  on top-20 fused candidates. Adds ~100–300ms, materially improves precision.

**Non-goals**:

- Pure semantic search alone (loses proper nouns + exact numbers).
- Pure keyword alone (no concept-level matching).
- Replacing Tier 1 with RAG for questions the schema can answer directly.

### 6.6 LLM-queryable database design

End goal: an analyst types *"top 5 MENA fintechs with stale MRR metrics"*;
an LLM agent produces correct SQL over the DB and a clean chart, without
hand-holding.

This works **only** if the schema is designed with LLM introspection in mind.

**The 12 rules**:

| # | Rule | Why |
|---|---|---|
| 1 | Tables and columns read like English. `company_metric_value`, not `cmv`. `captured_at`, not `ts`. | LLMs tokenize identifiers literally; abbreviations hurt. |
| 2 | `COMMENT ON TABLE` / `COMMENT ON COLUMN` for every public-facing object. | LLMs consume comments via `information_schema`. |
| 3 | **Denormalized analytics views** (`analytics.company_scorecard`, `analytics.recent_observations`) for common questions. | LLMs query one view instead of joining 6 tables — far fewer SQL errors. |
| 4 | Enumerated values via CHECK constraints with literal strings (`pipeline_stage IN ('deal_sourcing',…)`), not integer codes. | LLMs can introspect valid values and never invent strings. |
| 5 | Star-schema clarity: explicit dimensions (company, metric, extractor) vs facts (metric_observation, context_fact, job_run). | LLM SQL-generation is heavily trained on star schemas. |
| 6 | Never rename live columns. Add new + deprecate via comment. | LLM memory of schema is fragile to renames. |
| 7 | A curated **SQL cookbook** (`analytics/cookbook.yaml`) with ~30 example Q→SQL pairs. | Few-shot examples halve hallucinated SQL. |
| 8 | `timestamptz` everywhere. Never Unix ints in strings. | LLMs handle `timestamptz + INTERVAL` math natively. |
| 9 | Boolean columns named naturally: `is_override`, `has_prior_exit`, `linkedin_verified`. | Queries read naturally: `WHERE linkedin_verified = TRUE`. |
| 10 | JSONB columns with a docstring example (`context_fact.dimensions` comment: `'e.g. {"region": "MENA", "industry": "Fintech"}'`). | LLM knows the shape → writes `dimensions->>'region'` correctly. |
| 11 | Materialized views for expensive recurring queries (`company_score_view`), refreshed on write. | LLM query hits one view; no joining across the scoring pipeline. |
| 12 | A narrative `schema.md` — plain English: "when to use which table, common joins, anti-patterns". | The human-readable map for both humans and LLM agents. |

**Two concrete artifacts** will live in `analytics/`:

**A. `analytics/views.sql` — denormalized views in their own schema.**

```sql
CREATE SCHEMA IF NOT EXISTS analytics;

-- One row per company with score + provenance + pipeline state.
-- Use this for "top N", "stale metrics", pipeline dashboards.
CREATE VIEW analytics.company_scorecard AS
SELECT
    c.id                         AS company_id,
    c.name                       AS company_name,
    c.country,
    c.industry,
    c.pipeline_stage,
    c.source_channel,
    c.discovery_source_url,
    c.discovery_grounding_score,
    cs.overall_score,
    cs.must_have_score,
    cs.missing_must_haves,
    c.updated_at                 AS company_updated_at
FROM company c
LEFT JOIN company_score_view cs ON cs.company_id = c.id;

COMMENT ON VIEW analytics.company_scorecard IS
'One row per company with its current score, pipeline stage, and discovery
provenance. Primary view for "top N" and pipeline-analytics questions.';
```

Agent asked *"top 5 MENA fintechs"* generates:

```sql
SELECT company_name, overall_score
FROM analytics.company_scorecard
WHERE industry = 'Fintech'
  AND country IN ('UAE','Saudi Arabia','Egypt','Jordan','Bahrain','Kuwait','Oman','Qatar')
ORDER BY overall_score DESC NULLS LAST
LIMIT 5;
```

Clean. One view. No joins.

**B. `analytics/cookbook.yaml` — curated Q→SQL pairs.**

```yaml
- question: Top N companies by overall score
  tags: [leaderboard, scorecard]
  sql: |
    SELECT company_name, overall_score
    FROM analytics.company_scorecard
    ORDER BY overall_score DESC NULLS LAST
    LIMIT {n};
  chart: bar
  x_col: company_name
  y_col: overall_score

- question: Companies whose MRR observation is older than 90 days
  tags: [freshness, metrics]
  sql: |
    SELECT c.name, o.captured_at
    FROM metric_observation o
    JOIN metric m ON m.id = o.metric_id
    JOIN company c ON c.id = o.company_id
    WHERE m.name = 'Monthly Recurring Revenue'
      AND o.captured_at < NOW() - INTERVAL '90 days'
    ORDER BY o.captured_at ASC;
  chart: table
```

Agent flow: semantic search the cookbook for the 3 most similar questions
→ inject as few-shot examples into the SQL-generation prompt → generate +
execute. Zero-cost, dramatic reliability boost.

**C. `analytics/charts.py` — narrow charting contract for the LLM.**

```python
# The ONLY charting functions the LLM agent may call.
# Narrow surface = LLM cannot hallucinate arguments that break at runtime.
def bar(df: pd.DataFrame, x: str, y: str, title: str = "") -> go.Figure: ...
def line(df: pd.DataFrame, x: str, y: str, color: str = None, title: str = "") -> go.Figure: ...
def scatter(df: pd.DataFrame, x: str, y: str, color: str = None, size: str = None, title: str = "") -> go.Figure: ...
def table(df: pd.DataFrame) -> None: ...          # st.dataframe
def kpi(value, label: str, delta: float = None) -> None: ...   # st.metric
```

Agent generates:

```python
result_df = run_sql(generated_sql)
charts.bar(result_df, x='company_name', y='overall_score', title='Top 5 MENA Fintechs')
```

The agent's surface is 5 functions. Errors are impossible at the chart layer.

---

## 7. Entity resolution logic

Given a raw mention (filename, email sender, chunk text), resolve to a
`company_id` with confidence. Try rules in order; first hit wins.

| Rank | Rule | Confidence |
|---|---|---|
| 1 | Exact case-insensitive match on `company.name` | 1.0 |
| 2 | Exact match on `company_alias.alias` | 0.95 |
| 3 | Email domain matches `company.website` domain | 0.9 |
| 4 | Email domain matches `company_alias.alias` (type=email_domain) | 0.9 |
| 5 | Trigram similarity ≥ 0.85 on `company.name` | 0.75 |
| 6 | LLM inference (Gemini) with all matched candidates + context | 0.5–0.8 |
| 7 | None → flag for analyst review | — |

New `company_alias` rows are created:
- Automatically when discovery finds a company (alias = LinkedIn handle, legal name)
- Manually by analyst via UI
- By LLM at rule 6, pending analyst confirmation

---

## 8. Source priority for the value resolver

When multiple observations disagree on a metric value, pick the winner
by:

1. `override = TRUE` from analyst → always wins.
2. Higher source priority (rank lower = higher priority):

| Rank | Source | Rationale |
|---|---|---|
| 1 | `analyst_override` | Human judgment |
| 2 | `founder_interview` / `meeting_note` | Primary source |
| 3 | `pitchdeck` | Founder-authored |
| 4 | Regulated filings (Crunchbase, SEC) | Verified |
| 5 | `linkedin` enrichment | Self-reported |
| 6 | `alpha_scout` discovery | Tavily + LLM |
| 7 | News articles | Third-party observation |

3. Within same priority: most recent `captured_at` wins.
4. Tie-break: highest `confidence`.

The resolver runs on every `metric_observation` insert and updates
`company_metric_value`. Logic lives in a Python function
(`resolve_metric_value`) called from triggers or app code — NOT in raw
SQL (too complex to maintain as a stored procedure).

---

## 9. Phased roadmap

| Phase | Scope | Duration | Demoable outcome |
|---|---|---|---|
| **1. Foundation** | `source_document`, `source_chunk`, `extractor`, `extraction_run`, `metric_observation`. Refactor `company_metric_value` to derived. Migrate existing data as synthetic runs. Introduce `pipeline/` + `analytics/` directories. | ~2 days | Click any metric value → see its full provenance chain, including the originating document and chunk. |
| **2. Ingestion** | Dropbox watcher (polling). Two extractors: `pitchdeck_gemini_v1`, `meeting_note_md_v1`. `company_alias` + entity resolver. UI: document list per company. | ~3 days | Drop a PDF into Dropbox → auto-linked to company → metrics appear in scorecard. |
| **3. Context + scheduling** | `context_fact`, `context_dimension`, `scheduled_job`, `job_run`. `market_refresh.py` biweekly job. UI: market card per company. | ~2 days | One cron job refreshes 50 companies' market signals. UI shows "refreshed 3d ago". |
| **4. Hybrid retrieval (Tier 2)** | `pgvector` HNSW + Postgres FTS `tsvector` on `source_chunk`. RRF fusion function. Optional cross-encoder reranker. RAG grounding for gap agent. Semantic search box in UI. | ~3 days | Type "unit economics" in a company's search — hybrid retrieval returns best-ranked chunks across all sources, exact terms AND conceptual matches. |
| **5. LLM-queryable analytics** | `analytics` schema + denormalized views. `analytics/cookbook.yaml` (~30 Q→SQL). `analytics/charts.py` (narrow chart contract). Simple NL→SQL agent wired to the cookbook via few-shot. | ~3 days | Analyst types "top 5 MENA fintechs with stale MRR" → correct SQL, correct chart, zero hand-holding. |
| **6. Quality & ops** | `metric_validator` per metric type. Data quality dashboard. `job_run` observability page. `schema.md` narrative doc. | ~2 days | Dashboard: "3 metrics have stale data", "2 extractions failed yesterday". |

Total: ~2.5 weeks of focused work.

---

## 10. What we do NOT change

- `metric_type` / `metric` schemas — stable, seed-driven.
- Postgres `company_score_view` — still the single scoring query.
- Streamlit app structure.
- Discovery pipeline (`discovery.py`) — we wrap it as an extractor.

---

## 11. Risks & open questions

1. **Embedding cost**: ~1500 tokens per chunk × ~50 chunks per deck ×
   $0.02/1M tokens. Negligible. No concern.
2. **Dropbox API**: polling is simple but adds ~5min lag. Acceptable.
   Full real-time needs webhooks (Phase 2.5).
3. **pg_cron vs external cron**: `pg_cron` is simpler but ties scheduling
   to Postgres. External cron + Python is more flexible. Default:
   external cron until we need Postgres-native.
4. **Trigger-based vs app-based resolver**: Should
   `resolve_metric_value` run as a Postgres trigger on
   `metric_observation` insert, or be called from the app layer?
   Current preference: app layer (testable, debuggable). Revisit if we
   see race conditions.
5. **Backfill strategy for Phase 1**: Do we migrate existing
   `company_metric_value` rows into synthetic
   `metric_observation` + `extraction_run` rows, or wipe and start
   fresh? Default: backfill (preserves analyst work).
6. **Which source types in Phase 2?** Pitch decks and meeting notes
   chosen because they're highest-signal for analyst workflow. Notion
   and CRM follow in Phase 2.5.

---

## 12. Glossary

- **Bronze / Silver / Gold**: standard medallion layers. Raw / cleaned /
  analyst-ready.
- **Observation**: one atomic, immutable claim about a metric, tied to
  a source. Many observations → one canonical value.
- **Context fact**: a fact that applies to many companies (e.g. market
  CAGR by region).
- **Extractor**: a pure function `SourceDocument → [Observation]`.
- **Entity resolver**: function returning `(company_id, confidence)`
  for a raw mention.
- **Resolver / value resolver**: function that picks the winning
  observation per (company, metric) for the scorecard.

---

## 13. Repository structure

**Decision**: single repo (`company_scorer/`), not split into separate
projects. Shared schema, shared DB helpers, atomic PRs across producer
(platform) and consumer (UI), and one developer today — the overhead of
two repos is not justified.

**Discipline comes from directory structure, not repo boundaries**:

```
company_scorer/
├── app.py                          # Streamlit UI (consumer)
├── schema.sql                      # SINGLE source of truth for DB
├── db.py                           # Shared DB helpers
├── scoring.py                      # Postgres-side score view helpers
├── discovery.py                    # Existing; wrapped as an extractor adapter in Phase 2
├── founder_linkedin.py             # Existing
│
├── pipeline/                       # NEW (Phase 1) — data pipeline (producer, the "platform" layer)
│   ├── ingestion/
│   │   ├── dropbox_watcher.py      # Phase 2
│   │   └── manual_upload.py        # Phase 2
│   ├── extractors/
│   │   ├── base.py                 # Extractor interface
│   │   ├── pitchdeck_v1.py         # Phase 2
│   │   ├── meeting_note_v1.py      # Phase 2
│   │   └── alpha_scout_adapter.py  # Phase 2 (wraps discovery.py)
│   ├── entity_resolver.py          # Phase 2
│   ├── value_resolver.py           # Phase 1
│   └── jobs/
│       ├── market_refresh.py       # Phase 3
│       └── news_sweep.py           # Phase 3
│
├── analytics/                      # NEW (Phase 5) — LLM-friendly layer
│   ├── views.sql                   # analytics.* schema definitions
│   ├── cookbook.yaml               # ~30 curated Q→SQL pairs
│   ├── charts.py                   # Narrow chart contract
│   └── nl_to_sql.py                # NL-to-SQL agent
│
├── agents/                         # Existing
│   ├── gap_agent.py
│   └── market_agent.py             # Subsumed by jobs/market_refresh.py in Phase 3
│
├── ARCHITECTURE.md                 # This file
├── README.md
├── DEMO.md
└── schema.md                       # NEW (Phase 6) — narrative schema docs
```

**Boundary rules** (enforced in code review, not by the repo layout):

1. `pipeline/*` **writes** to Postgres; `app.py` **reads** only.
2. `analytics/*` is **read-only** over the pipeline's output — it must never reach back into `pipeline/`.
3. Extractors in `pipeline/extractors/` must implement `base.Extractor` — NO exceptions, NO ad-hoc shapes.
4. New tables touching `metric_observation` / `company_metric_value` require an update to both `schema.sql` AND `ARCHITECTURE.md` in the same PR.

**When to split into two repos** (future):

- A second developer joins working only on the platform side.
- Deployment targets diverge (platform → worker VM or cron host; UI → Streamlit Cloud / Vercel).
- The platform starts to serve a second downstream app.

---

## Appendix A: Full schema diagram (target state)

```
                ┌──────────────┐
                │   company    │◀───────────────┐
                └──────┬───────┘                │
                       │                         │
         ┌─────────────┴──────────┐              │
         ▼                        ▼              │
  ┌──────────────┐        ┌───────────────┐     │
  │ company_alias│        │  founder      │     │
  └──────────────┘        └───────────────┘     │
                                                 │
 ┌─────────────────┐      ┌───────────────────┐ │
 │ source_document │──┐   │ extractor         │ │
 └───────┬─────────┘  │   └────────┬──────────┘ │
         │            │            │             │
         ▼            ▼            ▼             │
 ┌─────────────┐  ┌─────────────────────┐       │
 │source_chunk │  │ extraction_run      │────┐  │
 └─────────────┘  └─────────┬───────────┘    │  │
                            │                 │  │
                            ▼                 │  │
                  ┌────────────────────┐      │  │
                  │ metric_observation │──────┤  │
                  └─────────┬──────────┘      │  │
                            │                 │  │
                            ▼                 │  │
                  ┌────────────────────┐      │  │
                  │ company_metric_value│─────┴──┘
                  └────────────────────┘
                            ▲
                            │
                  ┌────────────────────┐
                  │ context_fact       │
                  └────────────────────┘

   ┌─────────────────┐     ┌──────────────┐
   │ scheduled_job   │────▶│  job_run     │
   └─────────────────┘     └──────────────┘
```

---

## Appendix B: Migration order for Phase 1

```sql
-- 1. Create new tables
CREATE TABLE source_document (...);
CREATE TABLE source_chunk (...);
CREATE TABLE extractor (...);
CREATE TABLE extraction_run (...);
CREATE TABLE metric_observation (...);

-- 2. Add FK columns to company_metric_value
ALTER TABLE company_metric_value
  ADD COLUMN winning_observation_id UUID REFERENCES metric_observation(id),
  ADD COLUMN derived_from_context_fact_id UUID; -- FK added in Phase 3

-- 3. Seed built-in extractors
INSERT INTO extractor (name, version, supported_source_types) VALUES
  ('analyst_override', '1.0', '{analyst_note}'),
  ('alpha_scout_v1',   '1.0', '{web_page}'),
  ('seed_data_v1',     '1.0', '{seed}');

-- 4. Backfill: every existing company_metric_value row becomes a
--    synthetic extraction_run + metric_observation.
--    (Python migration script; too complex for raw SQL.)

-- 5. Verify: for every company_metric_value, winning_observation_id IS NOT NULL
--    OR override = TRUE.
```
