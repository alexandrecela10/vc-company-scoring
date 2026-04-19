"""Contracts shared by all preprocessors.

A Preprocessor's only job is: raw bytes -> ordered list of Chunks.
It MUST NOT call extractors, LLMs, or the database.

Why a dataclass + Protocol (not an ABC)?
  - Preprocessor is a structural interface: any callable with the right
    signature qualifies. We never instantiate "Preprocessor" directly.
  - Keeps individual preprocessors free of inheritance boilerplate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


@dataclass
class Chunk:
    """A single addressable text span from a source document.

    - `text`     : cleaned, chunk-local text the extractor will scan.
    - `locator`  : human-readable pointer ("slide_7", "section:Financials",
                   "message:3"). Written straight into source_chunk.locator
                   and shown in the UI evidence expander.
    - `ordinal`  : 0-based position within the source document. Used for
                   stable ordering; the locator is for humans.
    - `metadata` : free-form per-source-type extras (page_number, bbox,
                   section_title, sender, etc.). Not indexed by the DB;
                   stored as JSONB on source_chunk for future retrieval.
    """
    text: str
    locator: str
    ordinal: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class Preprocessor(Protocol):
    """Structural interface every preprocessor must satisfy."""

    # A short identifier matching source_document.source_type.
    # Declared as a class attribute, not a method, so registration is cheap.
    source_type: str

    def preprocess(self, raw_bytes: bytes, origin_path: str) -> List[Chunk]:
        """Turn raw file bytes into an ordered list of Chunks.

        Args:
            raw_bytes:   The unmodified file contents.
            origin_path: Filesystem path or URI of the source. Used only for
                         logging + entity resolution hints (e.g. filename).
                         The preprocessor MUST NOT read from it again.

        Returns:
            Chunks in document order. Empty list is valid (e.g. image-only
            PDF with no extractable text) and the orchestrator will handle
            it by creating a gap_action instead of observations.
        """
        ...
