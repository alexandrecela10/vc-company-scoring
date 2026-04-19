# analytics/ — LLM-friendly read layer (Phase 5 placeholder)

This package is the **read side** for analyst questions. It is designed
so an LLM agent can reliably produce correct SQL + charts from a
natural-language question.

Currently empty — scaffolded now to lock in the directory boundary.
Implementation begins in Phase 5 (see `ARCHITECTURE.md §9`).

## Planned contents

| File | Role |
|---|---|
| `views.sql` | Denormalized `analytics.*` schema: `company_scorecard`, `recent_observations`, `pipeline_funnel`, etc. One view per common analyst question pattern. |
| `cookbook.yaml` | Curated Q→SQL examples (~30 entries). Used as few-shot context for the NL→SQL agent. |
| `charts.py` | Narrow charting contract (5 functions: `bar`, `line`, `scatter`, `table`, `kpi`). The LLM agent can only call these. |
| `nl_to_sql.py` | Natural-language → SQL agent. Retrieves top-k relevant cookbook examples, injects as few-shot, generates SQL, validates, executes. |

## Boundary rules

1. `analytics/` is **read-only** over the database.
2. `analytics/` must NEVER reach back into `platform/`.
3. Every analytics view has a `COMMENT ON VIEW` so LLMs can introspect it.
4. Breaking changes to analytics views require a deprecation notice in the
   cookbook before removal.
