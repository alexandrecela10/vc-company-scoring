"""pitchdeck_v1 -- orchestrator that ties preprocessing, extraction, and persistence.

This is the Bronze -> Silver transformation for pitch-deck PDFs.

Flow (per call):
    1.  Hash the bytes + upsert source_document (idempotent on content_hash).
    2.  Preprocess bytes -> per-slide Chunks.
    3.  Persist chunks -> source_chunk rows; capture {locator: chunk_id}.
    4.  Start extraction_run (status='running').
    5.  DeterministicEngine.extract(chunks)                  -> deterministic obs
    6.  Missing = configured metrics - {obs.metric_name for obs}
    7.  LLMFallback.extract_missing(chunks, missing)         -> llm obs
    8.  Translate Observations -> metric_observation rows    (mapped by code)
    9.  Persist observations in one bulk insert.
    10. Finish extraction_run (status='success').
    11. Trigger value_resolver so winning Gold-layer values refresh.

Safety:
  - Any exception during 1..9 flips run status to 'error' + records message.
    We never leave a run stuck in 'running'.
  - dry_run=True short-circuits after step 7 (no DB writes, no run row).
    Used by the upload UI to preview observations before the analyst confirms.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import db
from pipeline.extractors.base import Observation
from pipeline.extractors.deterministic import DeterministicEngine
from pipeline.extractors.llm_fallback import LLMFallback
from pipeline.extractors.llm_provider import LLMProvider
from pipeline.preprocessors.base import Chunk
from pipeline.preprocessors.pitch_deck import PitchDeckPreprocessor, PreprocessingError

logger = logging.getLogger(__name__)

# Registry constants -- these must match seed.sql exactly.
EXTRACTOR_NAME = "pitchdeck_v1"
EXTRACTOR_VERSION = "1.0"
SOURCE_TYPE = "pitchdeck"
SOURCE_SUBTYPE = "pdf"

# Default rules file. Callers can override for A/B testing alternative rules.
DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules.yaml"


@dataclass
class ExtractionResult:
    """Return value of run_pitchdeck_extraction -- everything the caller needs."""
    observations: List[Observation] = field(default_factory=list)
    missing_metrics: List[str] = field(default_factory=list)
    extraction_run_id: Optional[str] = None
    source_document_id: Optional[str] = None
    chunk_count: int = 0
    source_document_created: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        """True if extraction ran without fatal errors (may still have 0 obs)."""
        return self.error is None


def run_pitchdeck_extraction(
    raw_bytes: bytes,
    company_id: str,
    *,
    origin: str = "manual_upload",
    origin_path: Optional[str] = None,
    origin_url: Optional[str] = None,
    ingested_by: Optional[str] = None,
    rules_path: Optional[Path] = None,
    provider: Optional[LLMProvider] = None,
    use_llm_fallback: bool = True,
    dry_run: bool = False,
) -> ExtractionResult:
    """Run the full pitchdeck_v1 pipeline for one uploaded deck.

    Args:
        raw_bytes:    The PDF bytes.
        company_id:   UUID of the company this deck belongs to. Caller is
                      responsible for entity resolution (step 8 in roadmap).
        origin:       How the file arrived ('manual_upload'|'dropbox'|...).
        origin_path:  Optional human-readable path for audit.
        origin_url:   Optional storage URL (e.g. Supabase Storage signed URL).
        ingested_by:  Analyst email / bot name for audit.
        rules_path:   Override for rules.yaml (defaults to repo's pipeline/rules.yaml).
        provider:     LLMProvider for the fallback. None -> GeminiProvider.
        use_llm_fallback: If False, skip the LLM step entirely (deterministic-only).
        dry_run:      If True, run preprocess + extract but do NOT write to DB.

    Returns:
        ExtractionResult. Check `.success` and `.error` before using observations.
    """
    result = ExtractionResult()
    rules = rules_path or DEFAULT_RULES_PATH

    # --- Step 1-2: preprocess (may fail on corrupt PDFs) ------------------
    try:
        chunks = PitchDeckPreprocessor().preprocess(raw_bytes, origin_path or "<bytes>")
    except PreprocessingError as e:
        result.error = f"preprocessing: {e}"
        logger.warning(result.error)
        return result

    result.chunk_count = len(chunks)
    if not chunks:
        # Image-only deck or entirely-blank PDF. Not an error, just nothing to extract.
        # We still return gracefully; caller may decide to create a gap_action.
        result.error = "no extractable text found in document"
        logger.info(result.error)
        return result

    # --- Step 5-7: run extractors (pure, no DB) ---------------------------
    # Deterministic first -- cheap, no LLM tokens burned.
    engine = DeterministicEngine.from_yaml(rules)
    det_obs = engine.extract(chunks)

    # Compute which configured metrics the deterministic pass did NOT hit.
    configured_metrics = list(engine._rules.keys())  # internal but we own the module
    extracted_codes = {o.metric_name for o in det_obs}
    missing = [c for c in configured_metrics if c not in extracted_codes]

    # LLM fallback (optional) -- only for metrics still missing.
    llm_obs: List[Observation] = []
    if use_llm_fallback and missing:
        if provider is None:
            # Lazy default so dry-runs without GEMINI_API_KEY still work.
            from pipeline.extractors.llm_provider import GeminiProvider
            provider = GeminiProvider()
        fallback = LLMFallback.from_yaml(rules, provider=provider)
        llm_obs = fallback.extract_missing(chunks, missing)

    all_obs = det_obs + llm_obs
    result.observations = all_obs
    # After both passes: anything still missing is a true gap.
    hit_codes = {o.metric_name for o in all_obs}
    result.missing_metrics = [c for c in configured_metrics if c not in hit_codes]

    if dry_run:
        return result

    # --- Step 3-4 + 8-10: persist to Bronze + Silver ---------------------
    try:
        result.source_document_id, result.source_document_created = _persist_source_doc(
            raw_bytes, origin, origin_path, origin_url, ingested_by
        )

        chunk_id_map = _persist_chunks(result.source_document_id, chunks)

        result.extraction_run_id = _start_run(
            result.source_document_id, company_id
        )

        inserted = _persist_observations(
            run_id=result.extraction_run_id,
            company_id=company_id,
            source_document_id=result.source_document_id,
            observations=all_obs,
            chunk_id_map=chunk_id_map,
        )

        db.finish_extraction_run(
            result.extraction_run_id,
            status="success",
            observations_count=inserted,
        )
    except Exception as e:
        # Mark the run failed so we never leave it stuck in 'running'.
        if result.extraction_run_id:
            try:
                db.finish_extraction_run(
                    result.extraction_run_id,
                    status="error",
                    error_message=str(e)[:500],
                )
            except Exception:
                pass
        result.error = f"persistence: {e}"
        logger.exception("pitchdeck_v1 persistence failed")
        return result

    # --- Step 11: refresh Gold layer -------------------------------------
    # value_resolver walks every metric for this company and picks the winning
    # observation (new pitchdeck_v1 obs may outrank older seed values).
    try:
        from pipeline.value_resolver import resolve_all_for_company
        resolve_all_for_company(company_id)
    except Exception as e:
        # Don't fail the whole extraction if resolver has issues; Silver is still
        # persisted. Just log so we notice.
        logger.warning(f"value_resolver failed post-extraction: {e}")

    return result


# ---------------------------------------------------------------------------
# Persistence helpers -- private to this module
# ---------------------------------------------------------------------------

def _persist_source_doc(
    raw_bytes: bytes,
    origin: str,
    origin_path: Optional[str],
    origin_url: Optional[str],
    ingested_by: Optional[str],
) -> tuple[str, bool]:
    """Upsert source_document by content hash. Returns (id, was_created)."""
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    row = db.get_or_create_source_document(
        content_hash=content_hash,
        source_type=SOURCE_TYPE,
        source_subtype=SOURCE_SUBTYPE,
        origin=origin,
        origin_path=origin_path,
        origin_url=origin_url,
        mime_type="application/pdf",
        size_bytes=len(raw_bytes),
        ingested_by=ingested_by,
    )
    return str(row["id"]), bool(row.get("_created"))


def _persist_chunks(source_document_id: str, chunks: List[Chunk]) -> dict:
    """Write source_chunk rows; return {locator: chunk_id}."""
    payload = [
        {
            "text": c.text,
            "locator": c.locator,
            "ordinal": c.ordinal,
            "page": c.metadata.get("page_number"),
        }
        for c in chunks
    ]
    return db.insert_source_chunks(source_document_id, payload)


def _start_run(source_document_id: str, company_id: str) -> str:
    """Look up the pitchdeck_v1 extractor, open a run row, return its id."""
    extractor = db.get_extractor_by_name(EXTRACTOR_NAME, EXTRACTOR_VERSION)
    if extractor is None:
        raise RuntimeError(
            f"Extractor {EXTRACTOR_NAME} v{EXTRACTOR_VERSION} not found; "
            f"did you apply seed.sql?"
        )
    return db.start_extraction_run(
        extractor_id=str(extractor["id"]),
        source_document_id=source_document_id,
        company_id=company_id,
    )


def _persist_observations(
    run_id: str,
    company_id: str,
    source_document_id: str,
    observations: List[Observation],
    chunk_id_map: dict,
) -> int:
    """Translate Observations into metric_observation rows and bulk-insert."""
    if not observations:
        return 0

    # Look up metric_id for every distinct code in one query.
    codes = list({o.metric_name for o in observations})
    code_to_metric_id = db.get_metric_ids_by_code(codes)

    rows = []
    for obs in observations:
        metric_id = code_to_metric_id.get(obs.metric_name)
        if metric_id is None:
            # Code in rules.yaml but no DB metric with that code. Log and skip;
            # the rest of the observations should still persist.
            logger.warning(
                f"No metric with code={obs.metric_name!r}; skipping observation"
            )
            continue
        rows.append({
            "metric_id": metric_id,
            "normalized_value": obs.value,
            "raw_value": obs.evidence_text,  # human-readable form of the claim
            "evidence_text": obs.evidence_text,
            "source_chunk_id": chunk_id_map.get(obs.chunk_locator),
            "confidence": obs.confidence,
        })

    return db.insert_metric_observations(
        run_id=run_id,
        company_id=company_id,
        source_document_id=source_document_id,
        rows=rows,
    )
