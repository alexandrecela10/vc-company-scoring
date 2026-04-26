# Threat Model for Company Scorer (Sovereignty + Grounding)

This document lists the major threat paths and the concrete controls planned to mitigate them.

## 1) Assets to protect

- Confidential source data (pitch decks, internal notes).
- PII-bearing content.
- Observation provenance and score integrity.
- API credentials and provider secrets.
- Audit trail correctness.

## 2) Threat actors

- External attacker abusing network boundaries.
- Malicious or compromised upstream content source.
- Prompt-injection content embedded in documents/web pages.
- Misconfigured internal service causing accidental data egress.
- Over-privileged analyst/service account.

## 3) Primary attack surfaces

- LLM provider calls.
- Web/news enrichment calls.
- Link verification/scraping connectors.
- Chat/assistant interfaces (present/future).
- Manual SQL or admin operations.

## 4) Risk register and mitigations

## 4.1 Data exfiltration via uncontrolled outbound calls

Risk:

- Components call arbitrary domains directly.

Mitigations:

- Route all outbound HTTP through `egress.py`.
- Enforce destination allowlist with purpose tags.
- Sovereign mode restricts to UAE-approved endpoints.
- Persist egress logs for alerting and audit.

## 4.2 Prompt injection from untrusted source content

Risk:

- Ingested text includes malicious instructions that alter agent behavior.

Mitigations:

- Input scrub gate before retrieval/reasoning.
- Treat source text only as data, never as executable instruction.
- Citation/quote verification for extracted claims.
- Rule-based and model-based jailbreak detectors.

## 4.3 Model swap / model drift without traceability

Risk:

- Provider/model changes silently alter outcomes.

Mitigations:

- Provider abstraction with explicit model routing.
- Pin and store `model_id`, `model_version`, `prompt_id`, `prompt_version` per run.
- Reproducibility checks via re-derive CLI.

## 4.4 Unauthorized access to sensitive chunks

Risk:

- User/tool retrieves restricted content without clearance.

Mitigations:

- Sensitivity labels on source docs/chunks.
- Clearance-aware retrieval filter.
- Deny-by-default policy for restricted data.
- Access attempts logged in audit trail.

## 4.5 Hallucinated or ungrounded claims influencing scores

Risk:

- LLM produces plausible but unsupported values.

Mitigations:

- Deterministic extraction first.
- LLM fallback guarded by quote/range checks.
- Reject non-grounded outputs.
- Keep immutable observations and resolver determinism.

## 4.6 Integrity loss through direct writes bypassing pipeline

Risk:

- Manual DB writes bypass evidence and policy checks.

Mitigations:

- Least-privilege DB roles.
- Write API boundaries with validation.
- Track all overrides with reason and actor.
- Red-team tests for unauthorized write paths.

## 5) Five-gates enforcement target

Every assistant turn must pass:

1. Identify: user identity + clearance.
2. Scrub: PII redaction + injection detection.
3. Retrieve: permission-filtered context only.
4. Reason: pinned model + allowed tools + citation policy.
5. Release: DLP scan + hallucination checks + audit log.

If a gate fails, response is blocked or redacted.

## 6) Residual risks

- Third-party outages and provider policy changes.
- False negatives in prompt-injection detection.
- Human operational mistakes in allowlist or role config.

Residual-risk posture:

- Fail closed where possible.
- Prefer explicit refusals over weakly grounded answers.
- Keep forensic-quality logs for post-incident analysis.
