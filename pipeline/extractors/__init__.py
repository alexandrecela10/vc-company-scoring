"""Extractors turn Chunks into Observations (candidate metric values).

One module per extractor family:
  - deterministic.py : the rules.yaml-driven regex + enum_map engine
  - llm_fallback.py  : LLM call with mandatory quote verification
  - pitchdeck_v1.py  : orchestrator that runs the two above in sequence

Extractors MUST NOT write to the database. Persistence is the orchestrator's
job (so extractors stay pure and unit-testable).
"""
from pipeline.extractors.base import Extractor, Observation

__all__ = ["Extractor", "Observation"]
