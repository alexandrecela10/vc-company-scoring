# Demo Walkthrough — Company Scorer

A 5-minute script for showing the system end-to-end.

## What this is

A deal-sourcing cockpit for a VC analyst. Every company gets a **single 0–5 score** rolled up from weighted dimensions (Founders Strength, Market Growth, Unit Economics, etc.), with **transparent, source-linked evidence** for every number. AI agents fill gaps automatically and the analyst stays in control via overrides and stage transitions.

## The three demo companies

| Company | State | Story |
|---|---|---|
| **Fintech A** | All metrics filled | Happy-path: fully scored with live news + founders |
| **Healthtech B** | 3 must-haves missing | Triggers the **Gap Agent** — drafts founder emails, runs web searches |
| **Climate C** | Stale market data | Triggers the **Market Signal Agent** — refreshes a value >180 days old |

## Before the demo

```bash
# Run the app
venv/bin/streamlit run app.py --server.port 8502
```

If the Market Signal Agent has already been run and Climate C is no longer stale, reset it:

```bash
venv/bin/python3 - <<'PY'
import db, market_agent
GG, METRIC = "44444444-0000-0000-0000-000000000003", "33333333-0000-0000-0000-000000000008"
db._execute("UPDATE company_metric_value SET is_latest=FALSE WHERE company_id=%s AND metric_id=%s", (GG, METRIC))
oldest = db._fetchone("SELECT id FROM company_metric_value WHERE company_id=%s AND metric_id=%s ORDER BY captured_at ASC LIMIT 1", (GG, METRIC))
db._execute("UPDATE company_metric_value SET is_latest=TRUE, value='false', captured_at=NOW() - INTERVAL '210 days' WHERE id=%s", (oldest["id"],))
print("Climate C is stale again.")
PY
```

## Walk-through (5 minutes)

### 1. Overview (sidebar) — 30s

Open http://localhost:8502. The sidebar shows all 3 companies with:

- Colour-coded score badge (red / amber / green)
- **⚠️ Incomplete** if any must-have is missing
- **⏳ Stale** if any market signal is >180 days old

> *Click Climate C — notice the ⏳ badge immediately tells you something is off.*

### 2. Climate C — Market Signal Agent — 60s

In the scorecard you'll see a **📈 Market Signal Agent** panel listing the stale metric. Click **"🔄 Refresh stale signals now"**:

1. **Tavily** searches the web for the current answer
2. **Gemini** extracts a boolean value + a verbatim quote + a source URL
3. **`link_verifier.py`** deterministically fetches the URL and confirms the quote is on the page
4. The new value is written with a **✅ Verified** badge in the evidence table

> *Key message: the AI cannot hallucinate sources — we check every link.*

### 3. Healthtech B — Gap Agent — 90s

Select Healthtech B. The **🤖 Gap Agent** panel shows 3 missing must-haves and how each will be handled:

- `Ask Founders` → drafts a founder email via Gemini
- `LinkedIn` / `Tavily` → runs a targeted web search

Click **"🤖 Run gap agent now"**. You'll see:

- 2 drafted emails in the action log
- 1 web-search attempt (no confident match for a fictional founder — expected)

The emails are **NOT auto-sent**. The analyst reviews, clicks **Mark as Sent**, and later **Mark Resolved** when the founder replies.

> *Key message: the agent closes information gaps; the analyst stays in the loop.*

### 4. Company intel block (any company) — 60s

Below the score header, every company shows:

- **🌐 Website · 💼 Company LinkedIn** — quick links
- **Founders** — names, titles, individual LinkedIn CTAs
- **📰 Recent news** — cached Tavily results (7-day TTL, refreshable)
- **Action buttons**:
  - **➡️ Move to First Contact** — logs a `pipeline_event` with the score snapshot
  - **🚫 Pass** — marks the deal passed
  - **📤 Share with team** — generates a copyable markdown brief (score + evidence + links)

### 5. Analyst override (Fintech A or Climate C) — 60s

Expand **✏️ Override a metric value (analyst lock)**. Pick any metric, type a new value + reason, save.

The row becomes **🔒 locked**:

- Score instantly recomputes
- Both agents skip this row — **forever**. We have two layers of protection:
  1. `find_stale_market_values()` excludes overridden rows → the agent doesn't even see them as stale
  2. Even if called directly, the agent refuses to overwrite `override=TRUE` rows

> *Key message: when humans know better than the data, they win — and we record why.*

### 6. Observability

Every LLM call (market agent extraction, gap agent drafting, web search extraction) is logged to **Langfuse** as a traced generation event with input/output/latency — so you can audit reasoning quality over time.

Open `https://cloud.langfuse.com` and you'll see:

- `market_agent_extract` generations per refresh
- `gap_agent_outreach_draft` per email drafted
- `gap_agent_web_extract` per search extraction

## Why this is differentiated

| Common VC tool | This system |
|---|---|
| "Score" is a black box | **Every score cell links to a verbatim quote + URL** |
| AI hallucinates sources | **Deterministic link verifier** — the quote must actually appear on the page |
| Agents silently overwrite analyst input | **`override=TRUE` rows are agent-proof** (two guard layers) |
| Stale data looks fresh | **⏳ Stale badge + one-click refresh** |
| No audit trail | **Every transition logged with a score snapshot** |

## Under the hood

- **Postgres (Supabase)** for all state — clean relational schema, no JSON blobs
- **Gemini 2.5 Flash** for extraction, drafting, analysis
- **Tavily** for web search and news
- **Langfuse** for LLM tracing
- **Streamlit** with `st.cache_data` + session-version invalidation — ~50ms click-to-render after the first load
