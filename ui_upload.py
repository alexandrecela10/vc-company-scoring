"""Streamlit Upload tab: PDF deck -> entity resolution -> extraction -> persist.

This is the first user-facing surface of the Phase 2a pipeline. It ties
together every piece we built:

    storage_client.upload_pdf(...)                 -- Bronze bytes
    PitchDeckPreprocessor.preprocess(...)          -- bytes -> chunks
    EntityResolver.from_db().resolve(...)          -- chunks -> company_id
    run_pitchdeck_extraction(..., dry_run=True)    -- preview Silver obs
    run_pitchdeck_extraction(..., dry_run=False)   -- persist Silver + Gold

UX flow:
    1. Analyst drops a PDF -> we hash + check for duplicates.
    2. We run the resolver on the deck's first slides and suggest a company.
       Analyst can accept the top candidate or pick from a dropdown.
    3. We run the extractor in dry_run mode and preview observations.
    4. Analyst clicks 'Save' -> we upload bytes + commit to DB.

Session state keys are prefixed 'upload.' so they don't collide with other tabs.
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from typing import List, Optional

import pandas as pd
import streamlit as st

import db
import storage_client
from pipeline.entity_resolver import (
    EntityResolver,
    _extract_domains,
    _tokenize_filename,
)
from pipeline.extractors.pitchdeck_v1 import run_pitchdeck_extraction
from pipeline.preprocessors.pitch_deck import PitchDeckPreprocessor, PreprocessingError

logger = logging.getLogger(__name__)

# Session state keys -- kept together so every reference is explicit.
K_FILE = "upload.file"
K_HASH = "upload.hash"
K_CHUNKS = "upload.chunks"
K_CANDIDATES = "upload.candidates"
K_COMPANY = "upload.company_id"
K_COMPANY_IS_NEW = "upload.company_is_new"  # True when we created the row just now
K_PREVIEW = "upload.preview"
K_COMMITTED = "upload.committed"


def render_upload_tab() -> None:
    """Main entry -- called from app.py inside `with tab_upload:`."""
    st.markdown("## 📤 Upload a pitch deck")
    st.caption(
        "Drop a PDF to extract structured metrics (MRR, funding stage, employee count, etc.). "
        "Every extracted value is grounded to the exact slide it came from."
    )

    uploaded = st.file_uploader(
        "PDF deck", type=["pdf"], key="upload.widget",
        help="We hash the file content, so re-uploading the same deck is a no-op.",
    )

    # --- Detect a new file vs. same file as before ---
    # Streamlit's uploader returns a NEW UploadedFile object every rerun, but
    # we only want to re-process when the BYTES change. Use the hash as the
    # identity check.
    if uploaded is None:
        _reset_state()
        return

    raw_bytes = uploaded.getvalue()
    content_hash = hashlib.sha256(raw_bytes).hexdigest()

    # New file dropped -> wipe stale session state from a previous upload.
    if st.session_state.get(K_HASH) != content_hash:
        _reset_state()
        st.session_state[K_HASH] = content_hash
        st.session_state[K_FILE] = {"name": uploaded.name, "bytes": raw_bytes}

    _render_file_summary(uploaded.name, raw_bytes, content_hash)

    # --- Warn if this deck was already ingested (idempotency guarantee) ---
    existing = storage_client.get_source_document_by_hash(content_hash)
    if existing:
        st.warning(
            f"This exact file was already ingested on "
            f"{existing['ingested_at']:%Y-%m-%d %H:%M} "
            f"(source_document.id = `{existing['id']}`). "
            "Re-saving will create a new extraction_run against the same document -- "
            "old observations stay, the resolver picks a new winner."
        )

    # --- Step 1: preprocess once so resolver + extractor can reuse chunks ---
    chunks = _get_or_preprocess_chunks(raw_bytes, uploaded.name)
    if chunks is None:
        return  # error already rendered

    # --- Step 2: entity resolution UI ---
    company_id = _render_entity_resolution_step(uploaded.name, chunks)
    if company_id is None:
        return  # analyst hasn't chosen yet

    # --- Step 3: extraction preview (dry_run) ---
    _render_extraction_preview_step(raw_bytes, company_id)

    # --- Step 4: commit to DB ---
    _render_commit_step(raw_bytes, company_id, uploaded.name)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_file_summary(name: str, raw: bytes, content_hash: str) -> None:
    """Small header showing what was uploaded."""
    size_kb = len(raw) / 1024
    col1, col2, col3 = st.columns(3)
    col1.metric("File", name)
    col2.metric("Size", f"{size_kb:,.0f} KB")
    col3.metric("SHA-256", content_hash[:12] + "...")


def _get_or_preprocess_chunks(raw_bytes: bytes, filename: str):
    """Run the preprocessor once per unique file; cache chunks in session state.

    Why once: preprocessing a 50-page PDF can take 1-3 seconds. Streamlit
    reruns the whole script on every widget interaction, so without caching
    we'd re-parse the PDF on every click. Keying by content_hash means a
    new file correctly busts the cache.
    """
    if st.session_state.get(K_CHUNKS) is not None:
        return st.session_state[K_CHUNKS]

    with st.spinner("Parsing PDF..."):
        try:
            chunks = PitchDeckPreprocessor().preprocess(raw_bytes, filename)
        except PreprocessingError as e:
            st.error(f"Could not parse this PDF: {e}")
            return None

    if not chunks:
        st.error(
            "No extractable text found. This might be an image-only scan -- "
            "we currently don't OCR. Ask the founder for a text-based PDF."
        )
        return None

    st.session_state[K_CHUNKS] = chunks
    return chunks


def _render_entity_resolution_step(filename: str, chunks) -> str | None:
    """Show resolver candidates and capture the analyst's company choice.

    Three paths, in priority order:
      1. HIGH confidence (>= 0.85): auto-accept top candidate; offer override.
      2. MID confidence (0.4 - 0.85): show shortlist of known companies.
      3. NO match: default to "Create new company" with a pre-filled name.
         This is the common case when growing the DB from new decks.
    """
    st.markdown("### Step 1 — Which company is this about?")

    # Lazy-load candidates once per file. Analyst can still override.
    if st.session_state.get(K_CANDIDATES) is None:
        resolver = EntityResolver.from_db()
        st.session_state[K_CANDIDATES] = resolver.resolve(filename, chunks)

    candidates = st.session_state[K_CANDIDATES]
    all_companies = db.get_all_companies()
    name_to_id = {c["name"]: str(c["id"]) for c in all_companies}

    # --- High confidence branch: auto-suggest with an override ---
    if candidates and candidates[0].score >= 0.85:
        top = candidates[0]
        st.success(
            f"**Detected:** {top.company_name}  "
            f"·  confidence **{top.score:.2f}**  "
            f"·  signals: {', '.join(top.signals)}"
        )
        use_top = st.checkbox(
            "Use this company", value=True, key="upload.use_top",
            help="Uncheck to pick manually or create a new company.",
        )
        if use_top:
            st.session_state[K_COMPANY] = top.company_id
            st.session_state[K_COMPANY_IS_NEW] = False
            return top.company_id
        # fall through to picker

    # --- Mid confidence: show shortlist as radio ---
    elif candidates:
        st.info("Resolver found possible matches — confirm or create new:")
        option_labels = [
            f"{c.company_name}  ({c.score:.2f})  ·  {', '.join(c.signals)}"
            for c in candidates
        ]
        option_labels.append("➕ None of these — create new company")
        pick = st.radio(
            "Candidates", option_labels, key="upload.pick_radio", index=None,
        )
        if pick is None:
            return None
        if not pick.startswith("➕"):
            idx = option_labels.index(pick)
            st.session_state[K_COMPANY] = candidates[idx].company_id
            st.session_state[K_COMPANY_IS_NEW] = False
            return candidates[idx].company_id
        # user picked "create new" -> fall through

    else:
        st.info(
            "No match against known companies — that's expected if this is "
            "a new deal. Create the company by default; adjust the name if needed."
        )

    # --- Default path: CREATE NEW COMPANY ------------------------------------
    # Pre-fill with the best guess from filename + first-slide text. Analyst
    # sees a single text input + Create button; no forced data entry otherwise.
    default_name = _suggest_company_name(filename, chunks)
    new_name = st.text_input(
        "New company name",
        value=default_name,
        key="upload.new_name",
        help="Only the name is required. Industry, country, website etc. "
             "can be filled in later from the scorecard.",
    )

    # Allow switching to 'pick existing' without scrolling away. Collapsed by
    # default so the primary action (create new) stays in focus.
    with st.expander("...or pick an existing company instead"):
        selected_name = st.selectbox(
            "Existing company",
            options=[""] + sorted(name_to_id.keys()),
            key="upload.manual_pick",
        )
        if selected_name:
            st.session_state[K_COMPANY] = name_to_id[selected_name]
            st.session_state[K_COMPANY_IS_NEW] = False
            return name_to_id[selected_name]

    if not new_name.strip():
        st.caption("Enter a name above and click Create to continue.")
        return None

    if st.button("➕ Create company and continue", key="upload.create_company",
                 type="primary"):
        try:
            row = db.create_company(new_name.strip())
        except Exception as e:
            st.error(f"Couldn't create company: {e}")
            return None
        company_id = str(row["id"])
        st.session_state[K_COMPANY] = company_id
        st.session_state[K_COMPANY_IS_NEW] = True

        # Seed aliases so future uploads auto-match this company:
        #  1. The name itself as a legal_name alias (lowercased by the resolver)
        #  2. Any domain-looking tokens from the first 3 slides as domain aliases
        _seed_aliases_for_new_company(company_id, new_name.strip(), chunks)

        # Bump the global version so the sidebar picks up the new company.
        st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1
        st.rerun()

    return None


def _suggest_company_name(filename: str, chunks) -> str:
    """Best-effort guess at the company name for a new-deck upload.

    Priority:
      1. First alpha token from filename (already de-noised by _tokenize_filename).
      2. First non-empty line of the first chunk, if short and title-like.
      3. Empty string -- analyst will type it.

    We pick filename first because it's usually cleaner than deck titles
    ("LUMEN - Series A Pitch Deck - 2024.pdf" -> 'Lumen' is easier than
    parsing "LUMEN // healthtech for everyone" from slide 1).
    """
    tokens = _tokenize_filename(filename)
    alpha_tokens = [t for t in tokens if t.isalpha()]
    if alpha_tokens:
        return alpha_tokens[0].capitalize()

    # Fallback: first line of first chunk, capped at a reasonable length.
    if chunks:
        first_line = (chunks[0].text or "").splitlines()[0].strip()
        # Reject lines that are clearly not names (too long, contains colons).
        if 0 < len(first_line) <= 40 and ":" not in first_line:
            return first_line
    return ""


def _seed_aliases_for_new_company(company_id: str, name: str, chunks) -> None:
    """Populate `company_alias` so future resolver calls recognise this company.

    This is the self-reinforcing loop: every upload of a new deck makes the
    next upload of a related deck match faster + with higher confidence.
    """
    # 1. Register the name itself as a legal_name alias (lowercased for the
    #    resolver's whole-word matching).
    try:
        db.insert_company_alias(company_id, name.lower(), "legal_name")
    except Exception:
        logger.exception("alias insert failed for name %s", name)

    # 2. Scan the first 3 chunks for domains (tabby.ai, lumen.health, ...).
    #    Use the same extractor the resolver uses so behaviour is consistent.
    head_text = "\n".join(c.text for c in sorted(chunks, key=lambda x: x.ordinal)[:3])
    for domain in _extract_domains(head_text):
        # Skip obvious junk like 'file.pdf' that our broad regex catches.
        if domain.endswith((".pdf", ".jpg", ".png", ".doc", ".docx")):
            continue
        try:
            db.insert_company_alias(company_id, domain, "domain")
            logger.info("seeded domain alias %s for new company %s", domain, name)
        except Exception:
            logger.exception("alias insert failed for domain %s", domain)


def _render_extraction_preview_step(raw_bytes: bytes, company_id: str) -> None:
    """Run the extractor in dry_run mode and show a preview table."""
    st.markdown("### Step 2 — Preview extracted observations")

    # Cache dry-run by (hash, company_id) so changing the company picker
    # re-runs but clicking elsewhere in the UI doesn't.
    cache_key = f"{st.session_state[K_HASH]}:{company_id}"
    cached = st.session_state.get(K_PREVIEW)
    if not cached or cached.get("key") != cache_key:
        if not st.button("Run extraction (dry-run)", key="upload.run_preview"):
            st.caption("Click to preview observations before saving.")
            return
        with _pipeline_progress_panel("Running extraction pipeline...") as cb:
            result = run_pitchdeck_extraction(
                raw_bytes, company_id, dry_run=True, progress_cb=cb,
            )
        st.session_state[K_PREVIEW] = {"key": cache_key, "result": result}
        cached = st.session_state[K_PREVIEW]

    result = cached["result"]
    if not result.success:
        st.error(f"Extraction failed: {result.error}")
        return

    _render_observations_table(result.observations)

    if result.missing_metrics:
        st.warning(
            "**Missing metrics** (no deterministic match, no LLM answer): "
            f"`{', '.join(result.missing_metrics)}`. "
            "We'll create gap actions for any must-haves among these after saving."
        )

    st.caption(
        f"Preprocessed **{result.chunk_count} slides**. "
        f"Extracted **{len(result.observations)}** observations. "
        f"_Nothing has been saved yet._"
    )


def _render_observations_table(observations: List) -> None:
    """Render a clean DataFrame of the dry-run observations."""
    if not observations:
        st.info("No observations extracted. Not necessarily a bug -- some decks "
                "are image-heavy or text-light. You can still save the Bronze record.")
        return

    rows = [
        {
            "Metric": o.metric_name,
            "Value": o.value,
            "Source": o.chunk_locator,
            "Confidence": f"{o.confidence:.2f}",
            "Method": o.method,
            "Evidence": (o.evidence_text or "")[:180] + (
                "..." if len(o.evidence_text or "") > 180 else ""
            ),
        }
        for o in observations
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )


def _render_commit_step(raw_bytes: bytes, company_id: str, filename: str) -> None:
    """Final 'Save' action -- uploads bytes + commits observations."""
    st.markdown("### Step 3 — Save to database")

    preview = st.session_state.get(K_PREVIEW)
    if not preview or not preview["result"].success:
        st.caption("Run the extraction preview first.")
        return

    committed = st.session_state.get(K_COMMITTED)
    if committed and committed.get("hash") == st.session_state[K_HASH]:
        _render_commit_result(committed["result"])
        return

    if not st.button("💾 Save to database", type="primary", key="upload.save"):
        return

    # --- Commit path: upload bytes + run orchestrator (not dry_run) -------
    content_hash = st.session_state[K_HASH]
    with _pipeline_progress_panel("Saving to database...") as cb:
        cb("Uploading bytes to Supabase Storage...")
        try:
            storage_path = storage_client.upload_pdf(content_hash, raw_bytes)
        except Exception as e:
            st.error(f"Storage upload failed: {e}")
            return
        cb(f"Uploaded to {storage_path}")

        result = run_pitchdeck_extraction(
            raw_bytes, company_id,
            origin="manual_upload",
            origin_path=filename,
            origin_url=storage_path,
            dry_run=False,
            progress_cb=cb,
        )

    st.session_state[K_COMMITTED] = {"hash": content_hash, "result": result}

    # Bump app-wide cache version so the sidebar + scorecard pick up new Gold values.
    st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1

    _render_commit_result(result)


def _render_commit_result(result) -> None:
    """Show what landed in the DB after a successful commit."""
    if not result.success:
        st.error(f"Save failed: {result.error}")
        return
    st.success(
        f"✅ Saved. "
        f"source_document.id = `{result.source_document_id}`, "
        f"extraction_run.id = `{result.extraction_run_id}`, "
        f"{len(result.observations)} observations persisted."
    )
    if result.missing_metrics:
        st.caption(
            f"Missing metrics (will be handled by gap agent): "
            f"`{', '.join(result.missing_metrics)}`"
        )
    # Offer a 1-click jump to the company's scorecard to close the loop
    # between "I uploaded this" and "here's what the scorecard says now".
    company_id = st.session_state.get(K_COMPANY)
    if company_id:
        if st.button("▶ View this company's scorecard", key="upload.goto_scorecard"):
            st.session_state.selected_company_id = company_id
            st.session_state.app_mode = "browse"
            _reset_state()
            st.rerun()


@contextmanager
def _pipeline_progress_panel(label: str):
    """Wrap st.status() so the orchestrator's progress_cb lands in a live panel.

    Yields a callable `cb(msg)` that the caller passes to `run_pitchdeck_extraction(..., progress_cb=cb)`.
    Each cb(msg) adds a line to the expandable status box. On exit the box
    auto-collapses into a 'Complete' state so the UI stays tidy afterwards.

    This is the only place we bind pipeline events to a UI surface -- keeping
    it in one helper means individual step renderers don't repeat the pattern.
    """
    status = st.status(label, expanded=True)
    def _cb(msg: str) -> None:
        # Each write is a new line in the status box; no need to track state.
        status.write(f"• {msg}")
    try:
        yield _cb
        status.update(label="Complete", state="complete", expanded=False)
    except Exception as e:
        status.update(label=f"Failed: {e}", state="error")
        raise


def _reset_state() -> None:
    """Clear everything from a previous upload so re-uploading is clean."""
    for k in (K_FILE, K_HASH, K_CHUNKS, K_CANDIDATES, K_COMPANY,
              K_COMPANY_IS_NEW, K_PREVIEW, K_COMMITTED):
        st.session_state.pop(k, None)
