# Sovereign Deployment Blueprint

This document describes how `company_scorer` can run in a UAE-sovereign mode while preserving grounded extraction and auditable scoring.

## 1) Sovereignty scope

Sovereignty in this project means:

- Data residency is explicit for every sensitive data flow.
- External egress is controlled by a single allowlist gate.
- Model provider routing is policy-driven (not hardcoded per caller).
- Every scored value is reproducible from immutable observations.
- Human override remains the final authority.

## 2) Current architecture baseline (today)

- Grounding is first-class: observations include evidence and provenance.
- Resolver is deterministic for winning values.
- Analyst override is sacred and not overwritten by automation.
- Temporal fields exist on `metric_observation` (`as_of_date`, `scenario`, etc.).

Known gaps to close for sovereign mode:

- No single outbound egress module.
- No data-jurisdiction tags on core tables.
- No model/prompt pinning metadata on extraction runs.
- No sovereign-mode deployment/runbook doc until now.

## 3) Data residency map (target)

### 3.1 Data classes

- `public`: public website/news summaries.
- `internal`: internal enrichment and analyst notes.
- `confidential`: investor materials (pitch decks, due diligence notes).
- `restricted`: PII-heavy or legally constrained data.

### 3.2 Residency policy

- `public` and `internal` may use approved global providers.
- `confidential` and `restricted` must remain in approved UAE sovereign perimeter.
- Data movement rules are enforced through egress allowlist + provider routing policy.

## 4) Model provider policy (target)

- Introduce provider abstraction (`LLMProvider`).
- Providers:
  - `GeminiProvider` (current baseline).
  - `FalconProvider` (UAE sovereign target via Core42/vLLM route).
  - `LocalProvider` (offline/air-gapped fallback).
- Every model call records: `model_id`, `model_version`, `prompt_id`, `prompt_version`.

## 5) Egress policy (target)

- All outbound HTTP goes through `egress.py`.
- `egress_allowlist.yaml` contains permitted destinations and purpose labels.
- Sovereign mode (`SOVEREIGN_MODE=true`) applies stricter UAE-only allowlist.
- Every egress call logs:
  - destination
  - bytes sent/received
  - jurisdiction
  - purpose
  - user/run context

## 6) Grounding + audit invariants (must never regress)

- No score without traceable observation chain.
- No observation accepted without source evidence text/url when required.
- LLM outputs that fail quote checks/range checks are rejected.
- All extraction runs are auditable (`extraction_run`, `metric_observation`, `pipeline_event`).

## 7) PII handling baseline (target)

- Detect and classify PII at ingest/scrub steps.
- Add `pii_level` metadata to source and metric-value layers.
- Restrict high-PII payloads to sovereign providers only.
- Log redaction actions and refusal events.

## 8) Deletion and retention process

- Deletion request must remove or tombstone:
  - source documents/chunks,
  - derived observations,
  - canonical metric values,
  - cached external calls where applicable.
- Keep non-sensitive audit metadata for compliance where policy requires.

## 9) UAE deployment topology (target options)

Primary target options:

- Core42-hosted inference + UAE-hosted Postgres.
- AWS `me-central-1` with self-managed Postgres (+ pgvector if needed).
- Hybrid with strict egress controls where allowed by policy.

Minimum sovereign mode components:

- Data store in UAE region.
- Provider endpoint(s) in UAE-approved perimeter.
- Egress allowlist enabled with sovereign-only destinations.
- Audit logs retained in-region.

## 10) Acceptance checklist for sovereign readiness

A run is considered sovereign-ready when all are true:

1. Non-allowlisted outbound request is blocked and logged.
2. Confidential/restricted chunks never route to non-sovereign provider.
3. Extraction run stores model and prompt version metadata.
4. Observation timeline preserves date + scenario per metric.
5. Score can be re-derived from immutable observations.
6. Override actions remain available and take precedence.
