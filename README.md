# Company Scorer — Jasoor Ventures

A structured, data-driven scoring system for ranking companies in the deal sourcing pipeline.

> 📖 **For a 5-minute walkthrough of the full demo, see [`DEMO.md`](./DEMO.md).**

## Architecture

```
Ingestion Layer   →   Interpretation Layer   →   Scoring Layer   →   UI
(Tavily / PDFs)       (Gemini extracts             (Option B           (Streamlit
                       metric values +              multiplicative      scorecard)
                       grounded evidence)           formula)
```

## Scoring Formula (Option B — Multiplicative Penalty)

```
metric_type_score  = weighted_avg(child metric values)
overall_score      = weighted_avg(metric_type_scores) × Π(must_have_score / 5.0)
```

A must-have metric scoring 1/5 caps the overall score at 20% of its potential.
If any must-have metric has **no value at all**, `overall_score = NULL` and a gap action is triggered.

## Setup

### 1. Install dependencies
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Fill in your GEMINI_API_KEY, TAVILY_API_KEY, LANGFUSE keys, SUPABASE keys
```

### 3. Set up the database
- Create a project at https://supabase.com
- Open the SQL Editor and run `schema.sql`
- Then run `seed.sql` to populate demo data

### 4. Run the app
```bash
streamlit run app.py
```

## Project Structure

```
company_scorer/
├── app.py               # Streamlit UI — scorecard, intel, pipeline, table browser
├── scorer.py            # Score engine — Option B multiplicative formula
├── db.py                # Supabase client + all DB read/write helpers
├── market_agent.py      # Tavily + Gemini market signal refresher
├── gap_agent.py         # Detects missing must-haves, fires Type A/B actions
├── company_intel.py     # News fetching + stage transitions + share summary
├── link_verifier.py     # Deterministic check that an evidence URL backs a quote
├── llm_client.py        # Gemini client (reused from Alpha Scout)
├── tracing.py           # Langfuse tracing (reused from Alpha Scout)
├── grounding.py         # Deterministic evidence validation (reused from Alpha Scout)
├── schema.sql           # Postgres schema — run once in Supabase SQL editor
├── seed.sql             # Demo data — 3 companies, partial metrics, gaps
├── reset.sql            # TRUNCATE helper for a clean reseed
├── requirements.txt
├── .env.example
└── README.md
```

## Key Tables

| Table | Purpose |
|---|---|
| `company` | One row per company (now includes `linkedin_url`) |
| `founder` | Company team members with LinkedIn profiles |
| `news_article` | Cache of Tavily news results (filled on-demand) |
| `data_source` | Known data sources (Crunchbase, LinkedIn, etc.) |
| `metric_type` | Dimension categories (Founders Strength, Tech Moat, etc.) |
| `metric` | Individual measurable metrics within each type |
| `company_metric_value` | **Core table** — measured values with evidence, URL, verification, override flag |
| `user_weight` | Per-analyst weights per metric type (defaults to house weight) |
| `pipeline_event` | Stage transition history with score snapshot |
| `gap_action` | Logged actions for missing must-have metrics |

## Features

### Scorecard tab
- **Overall score + completeness** at the top
- **Company intel block**: website, company LinkedIn, founders with LinkedIns, recent news (cached 7 days via Tavily), action buttons (Move to First Contact / Pass / Share with team)
- **Dimension breakdown** with per-metric evidence — each row shows the quote, a clickable **Link** to the source, and a **Verified** badge (✅ / ⚠️ / —) set by `link_verifier.py`
- **📈 Market Signal Agent** — detects Market Growth rows older than 180 days and offers a one-click refresh (Tavily + Gemini + link verification)
- **🤖 Gap Agent** — for each missing must-have metric:
    - `Ask Founders` → drafts an outreach email via Gemini
    - `Tavily` / `LinkedIn` / `Web Search` → fills the value via Tavily + Gemini with verified source
- **✏️ Override editor** — analyst-locked rows (`override=TRUE`) that agents cannot overwrite

### Pipeline tab
- Stage history with score snapshots
- Manual transitions between stages

### Table Browser tab
- Raw view of any of the DB tables (transparency for the analyst)

## Agent design (shared pattern)

Every LLM call goes through `llm_client.call_gemini()` which:
- Uses Gemini 2.5 Flash by default
- Traces every call to Langfuse as a generation event
- Parses JSON responses robustly

For web-sourced metrics, agents **never trust the LLM's citations blindly**:
1. Gemini returns `value + source_url + evidence_quote`
2. `link_verifier.verify()` fetches the URL, strips HTML, and confirms the quote appears verbatim
3. `url_verified` is persisted alongside the value so the UI can flag hallucinated citations

## Performance

The Streamlit UI caches expensive reads (`st.cache_data`) keyed by a session-state `data_version` counter. Any mutating action (agent run, override, stage change, news refresh) bumps the version, invalidating caches exactly when data changes. Typical click-to-render goes from ~4s (all DB queries) to ~50ms (cache hit).

## Environment variables

See `.env.example`. Required:
- `DATABASE_URL` — Supabase Postgres connection string (pooler)
- `GEMINI_API_KEY` — Google AI Studio key for the interpretation layer
- `TAVILY_API_KEY` — for market agent + gap agent + news fetching
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` — optional, for LLM tracing
