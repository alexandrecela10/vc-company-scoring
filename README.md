# Company Scorer

**A grounded data foundation for VC deal-sourcing AI workflows.**

This repository is two things at once:

1. A working Streamlit app that scores companies in a deal-sourcing pipeline.
2. A **provenance-first data platform** designed so AI workflows built on top are faster, more precise, and never hallucinate.

If you're here to build AI workflows on top of this data, read on — the architecture is specifically designed to make your job easier.

> For a 5-minute product walkthrough, see [`DEMO.md`](./DEMO.md).

---

## 1. Why this exists (problem statement)

VC deal sourcing generates huge amounts of unstructured context per company — pitch decks, meeting notes, news, LinkedIn profiles, Crunchbase dumps, founder emails. AI workflows on top of this (scoring, gap-filling, alerting, comparison, memo-drafting) live or die on **whether the inputs are trustworthy**.

Most "AI company database" projects fail because:

- They let the LLM be the source of truth. Hallucinations contaminate every downstream call.
- They mash facts from different sources without tracking which source said what.
- They don't distinguish "analyst said so" from "Crunchbase said so 2 years ago" from "Gemini guessed".
- They can't re-extract yesterday's deck with today's improved rules.

This repo is engineered to prevent all four failure modes by design.

---

## 2. Architectural principles

### 2.1 Medallion data layout (Bronze → Silver → Gold)

```
Bronze          Silver                   Gold
------          ------                   ----
source_document metric_observation       company_metric_value
source_chunk    (append-only, many       (canonical value per
(raw files +    rows per fact, one       (company, metric),
 per-page       per (run, source,        picked by the resolver)
 chunks)        metric, company))
```

- **Bronze** is **what we saw**: raw bytes + per-page text chunks. Append-only.
- **Silver** is **what sources claim**: every observation is an immutable row pointing at an extractor, a run, and the exact source chunk + evidence quote. Many rows per `(company, metric)` pair — disagreements are preserved, never overwritten.
- **Gold** is **what we believe**: exactly one canonical value per `(company, metric)`, chosen by the resolver from the Silver observations (or set manually by an analyst override). This is what the UI reads and what AI workflows consume by default.

**Why this matters for AI workflows**: a downstream agent can trust Gold values without rechecking them, while always being able to drill back through `winning_observation_id → source_chunk_id → source_document_id` to see the exact quote that produced the value. No more "ask the LLM again because we don't know where this number came from".

### 2.2 Grounding invariant (enforced in code)

Every extracted value in Silver must satisfy:

```
evidence_text must be a LITERAL SUBSTRING of the source chunk.
```

This invariant is enforced at three layers:

- **Deterministic extractors** (`pipeline/extractors/deterministic.py`): the match itself is the substring, by construction.
- **LLM fallback** (`pipeline/extractors/llm_fallback.py`): the model's `evidence_quote` is verified against the chunk text; a paraphrased quote is rejected and no row is written.
- **Web-sourced extractors** (`link_verifier.py`): the cited URL is fetched, HTML-stripped, and searched for the verbatim quote.

**Why this matters for AI workflows**: any claim you read from Silver or Gold can be rendered with its exact source quote in milliseconds. You can build verification UIs, audit logs, and "why did you say this?" tooltips for free.

### 2.3 Provenance chain (every fact is traceable)

```
company_metric_value          (Gold: the canonical value)
   └─ winning_observation_id → metric_observation      (Silver: one of N claims)
      └─ extraction_run_id   → extraction_run          (when / how)
         └─ extractor_id     → extractor               (which code version)
      └─ source_chunk_id     → source_chunk            (Bronze: the exact text)
         └─ source_document  → source_document         (Bronze: the file)
```

One function (`db.get_provenance_chain`) walks the entire chain. Build audit trails, compliance reports, or "re-extract with new rules" tooling on top of this.

### 2.4 Deterministic first, LLM only as fallback

Pitch-deck extraction is structured in two phases:

1. **Deterministic engine** (regex + enum_map + range validation) runs first. Cheap, auditable, no tokens burned.
2. **LLM fallback** runs only for metrics the deterministic pass did not extract. The fallback narrows context via keyword retrieval (not embeddings — overkill for 30-slide decks), calls the LLM once per missing metric, and rejects any response that fails the grounding invariant.

**Why this matters for AI workflows**: 70-80% of known metrics get extracted without any LLM cost and with perfect auditability. The LLM is used where it excels (reading "we have 35 engineers shipping weekly") and never where it's prone to fabricate (currency arithmetic, year-vs-zipcode confusion).

### 2.5 Provider-agnostic LLM layer

```
pipeline/extractors/llm_provider.py
  LLMProvider     (Protocol)         ← the contract
  GeminiProvider  (implementation)   ← today
  AnthropicProvider, OpenAIProvider  ← tomorrow, one new class each
```

Every LLM-touching component (LLM fallback, future agents) accepts an `LLMProvider`. Swapping models is a dependency-injection change, not a rewrite.

### 2.6 Extensibility contracts

| New thing you want to add | What you touch |
|---|---|
| New extraction rule for an existing metric | `pipeline/rules.yaml` — append a pattern |
| New metric | `pipeline/rules.yaml` + CLI registers it in DB + rules in one atomic step (coming in step 9) |
| New source type (e.g. meeting notes, Notion pages) | Implement `Preprocessor` + new orchestrator in `pipeline/extractors/<source>_v1.py` |
| New LLM provider | One new class implementing `LLMProvider.complete(prompt) -> str` |
| New scoring logic | `scorer.py` — Option B multiplicative formula isolated here |

None of these require DB schema changes (the schema was designed around the extractor/observation abstraction in Phase 1).

---

## 3. Where AI workflows plug in

The Gold layer (`company_metric_value`) is your read-side. The Silver layer (`metric_observation`) is your audit-side. Bronze (`source_document` + `source_chunk`) is your re-extraction-side.

Concrete entry points for AI workflows:

### 3.1 "Answer a question about a company"

```python
# Fast path: Gold has everything you need, canonically resolved.
import db
values = db.get_latest_values_for_company(company_id)
# Each row has: metric name, canonical value, evidence_text, source URL,
# url_verified flag, captured_at, override flag. You can render + cite
# without any further LLM call.
```

### 3.2 "Why did we say X?" (grounded explanations)

```python
chain = db.get_provenance_chain(company_id, metric_id)
# → returns joined cmv + observation + run + extractor + source_document.
# Feed this straight into a UI tooltip or an LLM "explain-this" prompt.
```

### 3.3 "Re-extract a deck with the new rules"

```python
from pipeline.extractors.pitchdeck_v1 import run_pitchdeck_extraction
result = run_pitchdeck_extraction(raw_bytes, company_id)
# Idempotent (content-hashed source_document; UNIQUE run-metric constraint).
# Old observations are preserved; the resolver picks the newest winner.
```

### 3.4 "Find the company this deck is about" (entity resolution)

```python
from pipeline.entity_resolver import EntityResolver
resolver = EntityResolver.from_db()
candidates = resolver.resolve(filename, chunks)
# Top candidate: candidates[0] has .company_id, .score, .signals.
# Signals like ["alias:tabby technologies ltd", "domain:tabby.ai"] explain WHY.
```

### 3.5 "Agent needs to fill in a missing value"

The `gap_agent.py` pattern: detect gaps, offer bounded actions (Tavily search, email draft, LinkedIn lookup). Every agent-produced value goes back through Silver with full provenance, so downstream workflows don't need to distinguish human-entered from agent-entered facts — the `extractor.name` on the observation tells them.

---

## 4. Data model summary

### Core tables (alphabetical; bolded = most-used)

| Table | Layer | Purpose |
|---|---|---|
| `company` | — | One row per company, plus `linkedin_url`, website |
| `company_alias` | — | Legal names, DBAs, domains, former names. Feeds entity resolver |
| **`company_metric_value`** | **Gold** | Canonical value per `(company, metric)`. Read by UI + AI workflows |
| `context_fact` | — | Dimensional facts (industry CAGR etc.) shared across companies (Phase 3) |
| `extraction_run` | Silver | One row per execution of an extractor. Audit unit |
| `extractor` | Silver | Registry: `(name, version)`. Versioned so old observations are preserved across rule changes |
| `founder` | — | Company team members |
| `gap_action` | — | Agent-driven actions to fill missing must-haves |
| `metric` | — | Individual measurable metrics. `metric.code` (snake_case) is the stable ID for rules.yaml |
| `metric_type` | — | Dimension categories (Founders Strength, Tech Moat, ...) |
| **`metric_observation`** | **Silver** | **Immutable, append-only fact table.** Every claim about `(company, metric)` is a row here |
| `news_article` | — | Cached Tavily news results |
| `pipeline_event` | — | Stage transition history with score snapshot |
| **`source_chunk`** | **Bronze** | Per-page (or per-slide) chunks of source documents. Embedded in Phase 4 |
| **`source_document`** | **Bronze** | One row per ingested file. Dedup keyed by SHA-256 of bytes |
| `user_weight` | — | Per-analyst weights per metric type |

### Key invariants

- `metric_observation` is **append-only**. Never `UPDATE` or `DELETE` a fact.
- `UNIQUE (extraction_run_id, company_id, metric_id)` on observations makes re-running idempotent.
- `company_metric_value.winning_observation_id` is the canonical pointer from Gold → Silver.
- `source_document.content_hash UNIQUE` means re-uploading the same PDF is a no-op.
- `metric.code` (stable snake_case) is the API key between `rules.yaml` and the DB. `metric.name` can change for UX; `code` must not.

---

## 5. Pipeline anatomy (Bronze → Silver → Gold, end-to-end)

```
1. UI receives a PDF (Streamlit upload tab)
   │
2. Entity resolver → company_id          [pipeline/entity_resolver.py]
   │   (filename + deck chunks + aliases + fuzzy match)
   │
3. PitchDeckPreprocessor bytes → chunks   [pipeline/preprocessors/pitch_deck.py]
   │   (one chunk per text-bearing slide)
   │
4. DeterministicEngine(chunks)            [pipeline/extractors/deterministic.py]
   │   → observations (regex + enum_map; range-validated)
   │
5. LLMFallback(chunks, missing_metrics)   [pipeline/extractors/llm_fallback.py]
   │   → observations (LLM call, quote-verified)
   │
6. Orchestrator persists Bronze + Silver  [pipeline/extractors/pitchdeck_v1.py]
   │   → source_document (content-hashed)
   │   → source_chunks (bulk insert)
   │   → extraction_run (audit trail)
   │   → metric_observations (bulk insert)
   │
7. value_resolver.resolve_all_for_company  [pipeline/value_resolver.py]
       → picks winning observation per (company, metric)
       → upserts Gold (company_metric_value)
```

Everything above is **idempotent**: content-hashed source documents, UNIQUE constraints on observations, upsert semantics on Gold. Re-uploading the same deck produces zero duplicate rows.

---

## 6. Repository layout

```
company_scorer/
├── app.py                         # Streamlit UI (scorecard, intel, pipeline, table browser)
├── scorer.py                      # Scoring engine (Option B multiplicative)
├── db.py                          # All DB helpers. psycopg2 + Supabase Postgres
├── pipeline/
│   ├── rules.yaml                 # Per-metric extraction rules (regex + enum + LLM config)
│   ├── value_resolver.py          # Silver → Gold: pick the winning observation
│   ├── entity_resolver.py         # Resolve uploaded docs → company_id
│   ├── preprocessors/
│   │   ├── base.py                # Chunk dataclass + Preprocessor contract
│   │   └── pitch_deck.py          # PDF → per-slide Chunks (pdfplumber)
│   ├── extractors/
│   │   ├── base.py                # Observation dataclass + Extractor contract
│   │   ├── deterministic.py       # regex + enum_map engine (no LLM)
│   │   ├── llm_fallback.py        # LLM-only-when-needed, quote-verified
│   │   ├── llm_provider.py        # Provider-agnostic LLM interface
│   │   └── pitchdeck_v1.py        # Orchestrator: preprocess → extract → persist → resolve
│   ├── migrate_phase2a_*.py       # Idempotent Phase 2a schema migrations
│   └── backfill_phase1.py         # Turn seed CMV rows into Silver observations
├── gap_agent.py                   # Detects missing must-haves, drafts outreach
├── market_agent.py                # Tavily + Gemini market signal refresher
├── company_intel.py               # News fetching + stage transitions
├── link_verifier.py               # Deterministic evidence-URL verification
├── llm_client.py                  # Gemini wrapper (retries, Langfuse tracing)
├── tracing.py                     # Langfuse integration
├── grounding.py                   # Shared grounding primitives
├── schema.sql                     # Postgres schema (run once in Supabase SQL editor)
├── seed.sql                       # Demo data (6 companies, partial metrics, gaps)
├── reset.sql                      # TRUNCATE helper for a clean reseed
├── tests/                         # unittest suite (36 tests, runs in <1s)
├── requirements.txt
├── .env.example
└── README.md
```

---

## 7. Scoring (product side)

### Formula (Option B — Multiplicative Penalty)

```
metric_type_score  = weighted_avg(child metric values)
overall_score      = weighted_avg(metric_type_scores) × Π(must_have_score / 5.0)
```

- A must-have metric scoring `1/5` caps the overall score at 20% of its potential.
- If any must-have has **no value at all**, `overall_score = NULL` and a gap action fires.

### Why multiplicative? 

VCs don't invest in companies that fail a must-have, no matter how strong they are elsewhere. An additive formula lets "great founders + great tech moat" paper over "no product-market fit evidence". Multiplication makes deficits visible.

---

## 8. Setup

### 8.1 Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 8.2 Configure

```bash
cp .env.example .env
# Fill in: GEMINI_API_KEY, TAVILY_API_KEY, LANGFUSE_*, SUPABASE_DB_URL
```

### 8.3 Database

1. Create a project at https://supabase.com
2. Open the SQL Editor, run `schema.sql`
3. Run `seed.sql` for demo data (6 companies, partial metrics)
4. (Optional) run `python3 -m pipeline.backfill_phase1` to materialize Phase 1 observations from seed data
5. (Optional) run `python3 -m pipeline.migrate_phase2a_company_alias` to seed domain aliases

### 8.4 Run

```bash
streamlit run app.py
```

### 8.5 Run tests

```bash
python3 -m unittest discover tests -v
```

---

## 9. Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_DB_URL` | yes | Postgres connection string (pooler URL from Supabase) |
| `GEMINI_API_KEY` | yes | Google AI Studio key — LLM extraction + agents |
| `TAVILY_API_KEY` | yes | Web search for market agent + gap agent + news |
| `LANGFUSE_PUBLIC_KEY` | no | LLM call tracing (recommended for debugging) |
| `LANGFUSE_SECRET_KEY` | no | Pair with above |
| `LANGFUSE_HOST` | no | Defaults to https://cloud.langfuse.com |

---

## 10. Current phase & roadmap

| Phase | Status | What it delivers |
|---|---|---|
| Phase 1 — Provenance foundation | ✅ Done | Bronze/Silver/Gold schema, extractor registry, provenance chain, value resolver. Backfilled from seed data |
| Phase 2a — Pitch-deck extraction | 🚧 ~80% | Preprocessor, deterministic engine, LLM fallback, orchestrator, entity resolver. **Pending:** metric CLI, Upload UI, gap-action creation on failure |
| Phase 2b — Meeting notes, emails, Notion | ⏳ Planned | New preprocessors; extractor orchestrators reuse the same `DeterministicEngine` + `LLMFallback` |
| Phase 3 — Context facts | ⏳ Planned | `context_fact` table: industry CAGR, geography multipliers, etc., shared across companies |
| Phase 4 — Embeddings + semantic retrieval | ⏳ Planned | Migrate `source_chunk.embedding` from `FLOAT[]` to `pgvector`; HNSW index; semantic search over Silver |

---

## 11. Notes for AI engineers

- **Read [`ARCHITECTURE.md`](./ARCHITECTURE.md)** for the deeper design rationale (source-priority ladder, resolver rules, override semantics).
- **Every DB write path has a helper in `db.py`.** Don't write SQL from new modules; add a helper instead.
- **Every LLM call should go through `llm_client.call_gemini()` or a future provider.** Langfuse tracing + retries are free that way.
- **The grounding invariant is load-bearing.** If you add a new extractor, your tests must assert evidence is a substring of the chunk text. See `tests/test_deterministic.py::test_evidence_is_substring_of_chunk`.
- **Prefer deterministic extraction when possible.** LLM fallback is a safety net, not a default.
- **Append, don't overwrite.** Silver is immutable. If a new run contradicts an old observation, both rows exist; the resolver picks the winner. This is what makes "re-run with new rules" a no-risk operation.

