"""Pitch deck preprocessor: PDF bytes -> one Chunk per text-bearing slide.

Design choices (documented so future contributors don't re-litigate them):

  - One chunk per page. Most decks are 1 slide per page. Multi-slide pages
    are rare enough to defer to Phase 2b if we ever see them in the wild.

  - Blank-page skipping (<20 chars). Page-number-only slides and title
    separators poison extraction (a giant regex for MRR would love to match
    "$12 M" in a random footer). Cost: slide_N locators become non-contiguous
    in the UI, which we accept. The `ordinal` field stays contiguous.

  - pdfplumber over pypdf/pymupdf:
      * pypdf text extraction is weaker on multi-column financial slides.
      * pymupdf is faster but AGPL-licensed (infectious for a closed tool).
      * pdfplumber is MIT, pure-Python, no system deps.

  - Raises PreprocessingError on corrupt PDFs so the orchestrator can log a
    pipeline_event + create a gap_action instead of crashing the upload flow.
"""
from __future__ import annotations

import io
import re
from typing import List

import pdfplumber

from pipeline.preprocessors.base import Chunk


class PreprocessingError(RuntimeError):
    """Raised when a source document cannot be preprocessed at all."""


# Collapse any run of whitespace (incl. CR/LF/NBSP) into a single space.
# Done AFTER pdfplumber because its default extract_text already inserts
# \n between lines — we want a single-line chunk for simpler regex.
_WS_RE = re.compile(r"\s+")


def _normalise_whitespace(text: str) -> str:
    """Collapse whitespace runs and trim. Preserves word boundaries."""
    return _WS_RE.sub(" ", text).strip()


class PitchDeckPreprocessor:
    """Turn a PDF pitch deck into a list of per-slide Chunks."""

    source_type = "pitchdeck"  # matches source_document.source_type enum in schema.sql

    # Pages shorter than this (after whitespace normalisation) are skipped.
    # Chosen empirically: title slides, section separators, and page-number
    # footers are consistently under 20 chars. Adjust if we see false skips.
    MIN_CHARS = 20

    def preprocess(self, raw_bytes: bytes, origin_path: str) -> List[Chunk]:
        """Extract one Chunk per text-bearing page of the PDF."""
        chunks: List[Chunk] = []

        # pdfplumber wants a file-like object, so wrap the bytes.
        try:
            pdf_ctx = pdfplumber.open(io.BytesIO(raw_bytes))
        except Exception as e:  # corrupt PDF, encrypted, zero-byte, etc.
            raise PreprocessingError(
                f"pdfplumber could not open {origin_path!r}: {e}"
            ) from e

        with pdf_ctx as pdf:
            for i, page in enumerate(pdf.pages):
                # extract_text() returns None on image-only pages.
                raw = page.extract_text() or ""
                cleaned = _normalise_whitespace(raw)

                # Drop near-empty pages. They add noise to extractors and
                # don't carry extractable signal. `ordinal` still counts
                # them implicitly via the page index `i`.
                if len(cleaned) < self.MIN_CHARS:
                    continue

                chunks.append(
                    Chunk(
                        text=cleaned,
                        locator=f"slide_{i + 1}",  # 1-indexed for humans
                        ordinal=i,
                        metadata={
                            "page_number": i + 1,
                            "page_width": float(page.width),
                            "page_height": float(page.height),
                            "char_count": len(cleaned),
                        },
                    )
                )

        return chunks
