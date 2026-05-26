# Company Intelligence — Grounded, Sovereign Deal Sourcing for VCs

> **A grounded, AI-sovereign company-intelligence engine for venture capital funds.**
> Track more companies, rank them automatically, and defend every score with full provenance.

---

## 1. Business Proposal

### The problem

VC deal sourcing is bottlenecked by manual triage. Analysts spend hours on inbox archaeology and shallow scans across decks, news, LinkedIn, and Crunchbase. Good companies sit untouched while competitors reach the founder first. Decisions rely on trust-me memos with no audit trail, and "AI tools" frequently hallucinate — making them unsafe for IC and LPs.

### The product

A company-intelligence platform that:

- **Ingests** pitch decks (inbound) and outbound signals (web, news, filings).
- **Extracts** structured, grounded observations — every fact tied to a source quote.
- **Scores** companies using transparent formulas — each component is auditable.
- **Ranks** the deal pipeline so analysts open their day with the right call list.
- **Tracks** changes over time — projections, actuals, and "what changed overnight".
- **Stays sovereign** — provenance + extractor versioning + designed-in data control.

### Who it’s for

| Persona | What they get |
|---|---|
| **VC Analyst** | Ranked queue, grounded scorecards, evidence on every claim, daily morning digest, IC-ready exports |
| **VC Fund CEO** | More companies tracked, faster pipeline velocity, defensible scoring, LP-ready reports, AI sovereignty posture |

### Business impact (user stories)

Sourced from the [`docs/jasoor_user_stories.pdf`](./docs/jasoor_user_stories.pdf):

| Persona | Advantage | Business outcome |
|---|---|---|
| VC Analyst | Grounded, source-linked company facts | Higher trust in data, fewer bad decisions |
| VC Analyst | Faster time-to-insight, fewer LLM calls | Lower cost, better tool adoption |
| VC Analyst | Ranked deal pipeline + must-have checks | Focus on best opportunities first |
| VC Analyst | Daily morning email with key changes | Faster reaction to market & company updates |
| VC Analyst | One place for financials, evidence, and scoring logic | Less context switching, faster IC prep |
| VC Fund CEO | More companies ingested and continuously tracked | Larger qualified top-of-funnel |
| VC Fund CEO | Faster progression through the sourcing pipeline | More efficient deal velocity |
| VC Fund CEO | Transparent, auditable scoring framework | Better governance & investment discipline |
| VC Fund CEO | LP-ready reporting (coverage, pipeline quality, score trends) | Stronger LP narrative & credibility |
| VC Fund CEO | AI-sovereign posture (data control, traceability) | Reduced compliance & reputation risk |

### Demo materials

- **Product walkthrough deck:** [`docs/jasoor_company_intelligence_demo.pdf`](./docs/jasoor_company_intelligence_demo.pdf)
- **User stories one-pager:** [`docs/jasoor_user_stories.pdf`](./docs/jasoor_user_stories.pdf)
- **5-minute live demo script:** [`DEMO.md`](./DEMO.md)

### North star

> Invest in as many great deals as possible, more often and faster than the competition — backed by data the fund can defend.

---

## 2. Architecture (high level)

The system is built on a **medallion data model** with a strict grounding invariant and append-only history. This is what makes scores defensible and AI workflows on top safe.

```
Bronze            Silver                       Gold
------            ------                       ----
source_document   metric_observation           company_metric_value
source_chunk      (immutable, time-series,     (canonical value per
(raw files +      one row per claim with       (company, metric),
 per-page         provenance + temporal         picked by resolver)
 chunks)          contract)
```

- **Bronze — what we saw**: raw bytes + per-page chunks. Append-only.
- **Silver — what sources claim**: every observation is immutable, with extractor + run + chunk + evidence quote + temporal contract (`as_of_date`, `scenario`, `period_label`, `currency`).
- **Gold — what we believe**: exactly one canonical value per `(company, metric)`, picked by the resolver (or set by analyst override). UI and AI workflows read this by default.

### Core principles

| Principle | Why it matters |
|---|---|
| **Grounding invariant** — `evidence_text` must be a literal substring of the source chunk | No hallucinations; every claim is verifiable |
| **Deterministic first, LLM as fallback** — regex + enum + table-aware patterns run before any LLM call | Lower cost, better auditability, faster |
| **Provenance chain** — value → observation → run → extractor → chunk → document | One query explains "why did we say this?" |
| **Append-only Silver** — never `UPDATE` or `DELETE` a fact; new observations land alongside old ones | Re-running with new rules is risk-free |
| **Time-series safe** — temporal contract on every observation; `metric_projection_vs_actual` view | Compare projections vs actuals; track drift |
| **Provider-agnostic LLM layer** — `LLMProvider` protocol with swappable implementations | Vendor-neutral; sovereignty-ready |
| **Override system** — analyst-locked values are never overwritten by agents; reasons captured | Analyst expertise becomes institutional memory |

### Sovereignty posture

This system is designed for jurisdictions that care about data residency, egress control, and auditability. See:
- [`SOVEREIGN_DEPLOYMENT.md`](./SOVEREIGN_DEPLOYMENT.md) — deployment topology, residency, provider abstraction
- [`THREAT_MODEL.md`](./THREAT_MODEL.md) — assets, actors, surfaces, mitigations

### Grounded scoring

The default scoring formula is **Option B — multiplicative penalty**:

```
metric_type_score  = weighted_avg(child metric values)
overall_score      = weighted_avg(metric_type_scores) × (min must_have_score / 5.0)
```

A must-have type scoring 1/5 caps the overall at ~20% of its potential. Missing must-haves → score is `NULL` and a gap action fires.

For data-rich types (Financials shipped first), a **grounded formula** replaces the generic average:

```
FinancialsScore = weighted_avg(
    gross_margin / 20,
    runway_months / 6,
    1 + 2 * (revenue / burn_rate),
    funding_stage
) → clamped 1–5 per component
```

Each input is sourced from a real `metric_observation`. The UI exposes the formula and per-component evidence in the scorecard.

---

## 3. Pipeline anatomy (end-to-end)

```
1. UI receives a PDF (Streamlit upload tab)
   ↓
2. Entity resolver → company_id          [pipeline/entity_resolver.py]
   ↓
3. PitchDeckPreprocessor bytes → chunks   [pipeline/preprocessors/pitch_deck.py]
   ↓
4. DeterministicEngine(chunks)            [pipeline/extractors/deterministic.py]
     regex + enum_map + range validation + table + layout patterns
   ↓
5. LLMFallback(chunks, missing_metrics)   [pipeline/extractors/llm_fallback.py]
     LLM call only for unfilled metrics; quote-verified against chunk
   ↓
6. Orchestrator persists Bronze + Silver  [pipeline/extractors/pitchdeck_v1.py]
     - source_document (content-hashed, idempotent)
     - source_chunks (bulk insert)
     - extraction_run (audit trail)
     - metric_observations (bulk insert with fingerprint + temporal contract)
   ↓
7. value_resolver.resolve_all_for_company  [pipeline/value_resolver.py]
     Picks winning observation per (company, metric); upserts Gold
```

Everything is **idempotent**: content-hashed source documents, observation fingerprints for time-series-safe inserts, upsert semantics on Gold. Re-uploading the same deck produces zero duplicate rows.

---

## 4. Data model summary

| Table | Layer | Purpose |
|---|---|---|
| `company` | — | One row per company; aliases in `company_alias` |
| `metric_type` / `metric` | — | Dimension catalog (Founders Strength, Tech Moat, ...) and individual metrics. `metric.code` is the stable API key |
| **`source_document`** | **Bronze** | Ingested files; dedup by SHA-256 |
| **`source_chunk`** | **Bronze** | Per-page/slide chunks (embedding-ready) |
| `extractor` | Silver | Registry of `(name, version)` |
| `extraction_run` | Silver | One row per extractor execution |
| **`metric_observation`** | **Silver** | Append-only fact table with temporal contract + `observation_fingerprint` |
| **`company_metric_value`** | **Gold** | Canonical value per `(company, metric)`; `winning_observation_id` points back to Silver |
| `gap_action` | — | Agent-driven follow-ups for missing must-haves |
| `pipeline_event` | — | Stage transition history with score snapshot |
| `user_weight` | — | Per-analyst weights per metric type |
| `news_article` | — | Cached web signals (Tavily) |

**Key invariants**

- `metric_observation` is append-only.
- `UNIQUE (observation_fingerprint)` — re-running an extractor is idempotent, while still allowing multiple time-series observations per `(company, metric)`.
- `company_metric_value.winning_observation_id` → canonical pointer Gold→Silver.
- `source_document.content_hash UNIQUE` → re-uploading the same PDF is a no-op.

---

## 5. Where AI workflows plug in

The **Gold** layer is the read-side. **Silver** is the audit-side. **Bronze** is the re-extraction-side.

```python
# Read canonical company facts (no LLM call needed)
import db
values = db.get_latest_values_for_company(company_id)

# Explain "why did we say this?"
chain = db.get_provenance_chain(company_id, metric_id)

# Re-extract a deck with new rules (idempotent)
from pipeline.extractors.pitchdeck_v1 import run_pitchdeck_extraction
result = run_pitchdeck_extraction(raw_bytes, company_id)
```

Any agent (gap, market, outbound) writes its outputs back through Silver with full provenance — downstream workflows don’t need to distinguish human from agent claims, the `extractor.name` says everything.

---

## 6. Repository layout

```
company_scorer/
├── app.py                         # Streamlit UI (scorecard, intel, pipeline, table browser)
├── scorer.py                      # Scoring engine (Option B + grounded formulas)
├── db.py                          # All DB helpers (psycopg2 + Supabase Postgres)
├── pipeline/
│   ├── rules.yaml                 # Per-metric extraction rules (regex + enum + LLM config)
│   ├── value_resolver.py          # Silver → Gold: pick the winning observation
│   ├── entity_resolver.py         # Resolve uploaded docs → company_id
│   ├── preprocessors/             # PDF → per-slide chunks (pdfplumber)
│   ├── extractors/                # Deterministic, LLM fallback, layout patterns, table-aware
│   └── migrate_phase*.py          # Idempotent schema migrations
├── discovery.py                   # Outbound discovery (Alpha Scout bridge)
├── gap_agent.py                   # Detects missing must-haves, drafts outreach
├── market_agent.py                # Tavily + LLM market signal refresher
├── company_intel.py               # News fetching + stage transitions
├── link_verifier.py               # Deterministic evidence-URL verification
├── llm_client.py                  # LLM wrapper (retries, Langfuse tracing)
├── tracing.py                     # Langfuse integration
├── grounding.py                   # Shared grounding primitives
├── schema.sql                     # Postgres schema (run once in Supabase)
├── seed.sql                       # Demo data
├── tests/                         # unittest suite (65+ tests)
├── docs/
│   ├── jasoor_company_intelligence_demo.pdf  # Product walkthrough
│   └── jasoor_user_stories.pdf               # Persona advantages + outcomes
├── DEMO.md                        # 5-min demo script
├── ARCHITECTURE.md                # Deeper design rationale
├── SOVEREIGN_DEPLOYMENT.md        # Sovereignty deployment blueprint
├── THREAT_MODEL.md                # Threat model
├── requirements.txt
├── .env.example
└── README.md                      # ← this file
```

---

## 7. Setup

### 7.1 Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 7.2 Configure

```bash
cp .env.example .env
# Fill in: SUPABASE_DB_URL, GEMINI_API_KEY, TAVILY_API_KEY, LANGFUSE_* (optional)
```

### 7.3 Database (Supabase Postgres)

1. Create a Supabase project.
2. Run `schema.sql` in the SQL editor.
3. Run `seed.sql` for demo data.
4. (Optional) `python3 -m pipeline.backfill_phase1` to materialize observations from seed data.
5. (Optional) `python3 -m pipeline.migrate_phase2d_timeseries_persistence` for time-series fingerprints.
6. (Optional) `python3 -m pipeline.migrate_phase2e_projection_vs_actual_view` for the projection-vs-actual view.

### 7.4 Run the app

```bash
streamlit run app.py
```

### 7.5 Run tests

```bash
python3 -m unittest discover tests -v
```

---

## 8. Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_DB_URL` | yes | Postgres connection string |
| `GEMINI_API_KEY` | yes | LLM extraction + agents |
| `TAVILY_API_KEY` | yes | Web search for market & gap agents and news |
| `LANGFUSE_PUBLIC_KEY` | no | LLM call tracing (recommended) |
| `LANGFUSE_SECRET_KEY` | no | Pair with above |
| `LANGFUSE_HOST` | no | Defaults to `https://cloud.langfuse.com` |

---

## 9. Roadmap

| Phase | Status | Delivers |
|---|---|---|
| Phase 1 — Provenance foundation | ✅ Done | Bronze/Silver/Gold schema, provenance chain, value resolver |
| Phase 2a–c — Pitch-deck extraction | ✅ Done | Preprocessor, deterministic engine, LLM fallback, table + layout patterns, temporal contract |
| Phase 2d — Time-series persistence | ✅ Done | Observation fingerprint, idempotent inserts, multiple temporal observations per metric |
| Phase 2e — Projection vs actual | ✅ Done | `metric_projection_vs_actual` view |
| Phase 3 — Grounded scoring (per type) | 🚧 Financials shipped; Founders, Market, Tech Moat next |
| Phase 4 — Outbound signals + daily run | ⏳ Planned | Continuous tracking + morning digest |
| Phase 5 — Model/prompt provenance + provider abstraction | ⏳ Planned | True AI-sovereign enforcement (not just docs) |
| Phase 6 — Multi-fund foundation | ⏳ Planned | `fund_id` scoping, metric templates + per-fund overrides |
| Phase 7 — Embeddings + semantic retrieval | ⏳ Planned | `pgvector` over Bronze chunks |

---

## 10. Notes for engineers

- **Read [`ARCHITECTURE.md`](./ARCHITECTURE.md)** for source-priority ladder, resolver rules, and override semantics.
- **Every DB write goes through `db.py`.** Don’t write SQL from new modules; add a helper.
- **Every LLM call goes through `llm_client`.** Tracing + retries are free that way.
- **The grounding invariant is load-bearing.** New extractors must assert evidence is a substring of the chunk text (`tests/test_deterministic.py`).
- **Prefer deterministic extraction.** LLM fallback is a safety net, not a default.
- **Append, don’t overwrite.** Silver is immutable. The resolver picks the winner.

---

## License & confidentiality

Confidential prototype. © Alexandre Cela, 2026. All rights reserved.
