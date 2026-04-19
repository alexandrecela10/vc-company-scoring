"""Preprocessors turn raw bytes from a source file into a list of Chunks.

One module per source_type (pitch_deck, meeting_note, email, ...).
Extraction rules are source-agnostic; preprocessing is where source-specific
quirks (PDF layout, email quote stripping, markdown section splitting) live.
"""
from pipeline.preprocessors.base import Chunk, Preprocessor

__all__ = ["Chunk", "Preprocessor"]
