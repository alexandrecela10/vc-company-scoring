# pipeline/ — Data pipeline (producer)

This package is the **write side** of the Company Scorer.

Everything that ingests data, extracts values, or mutates the database
lives here.

> **Naming note**: originally called `platform/` to match the "data
> platform" framing in `ARCHITECTURE.md`, but Python has a stdlib
> `platform` module — shadowing it breaks streamlit and google auth
> libs. Renamed to `pipeline/` for safety.

The Streamlit UI in `app.py` is the **read side** — it never reaches into
`pipeline/` directly.

## Current modules (Phase 1)

| Module | Role |
|---|---|
| `value_resolver.py` | Picks the "winning" `metric_observation` per `(company, metric)` and writes it to `company_metric_value.winning_observation_id`. Respects analyst overrides. |
| `backfill_phase1.py` | One-shot migration: turns every pre-existing `company_metric_value` row into a synthetic `extraction_run` + `metric_observation` so the provenance chain is complete. |

## Planned modules (Phase 2+)

| Module | Phase | Role |
|---|---|---|
| `ingestion/dropbox_watcher.py` | 2 | Polls a Dropbox folder, hashes new files, creates `source_document` rows. |
| `ingestion/manual_upload.py` | 2 | Streamlit uploader variant of the Dropbox flow. |
| `extractors/base.py` | 2 | Abstract base class — every extractor is a `SourceDocument → List[Observation]` function. |
| `extractors/pitchdeck_v1.py` | 2 | PDF → pdfplumber → Gemini → observations. |
| `extractors/meeting_note_v1.py` | 2 | Markdown → Gemini → observations. |
| `extractors/alpha_scout_adapter.py` | 2 | Wraps the existing `discovery.py` as a standard `Extractor`. |
| `entity_resolver.py` | 2 | Raw mention → `(company_id, confidence)` via a rule ladder (exact → alias → domain → fuzzy → LLM). |
| `jobs/market_refresh.py` | 3 | Bi-weekly cron: refreshes `context_fact` rows for each (region, industry) pair. |
| `jobs/news_sweep.py` | 3 | Daily cron: caches top 3 news per active company. |

## Boundary rules

1. `pipeline/` **writes** to Postgres; `app.py` **reads**.
2. Every extractor implements `pipeline/extractors/base.Extractor`.
3. Schema changes touching `metric_observation` / `company_metric_value`
   require an `ARCHITECTURE.md` update in the same PR.
