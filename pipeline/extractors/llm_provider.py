"""Provider-agnostic LLM interface.

Why this layer exists:
  We use Gemini today, but the fallback's parse/verify logic is provider-
  agnostic. Isolating the raw `complete(prompt) -> text` call behind a
  Protocol means adding Claude/OpenAI/local models later = one new class,
  zero changes to llm_fallback.py.

Contract:
  LLMProvider.complete() must return raw text. JSON parsing, retries,
  tracing are implementation-specific (Gemini wrapper handles retries
  + Langfuse; a Claude wrapper would handle its own).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol


class LLMProvider(Protocol):
    """Any LLM backend the fallback can talk to."""

    # Human-readable identifier stored on the Observation's method_details
    # so we can audit "this value came from gemini-2.5-flash".
    name: str

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        span_name: str = "llm_fallback",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send prompt, return raw text response.

        The provider MAY raise on total failure. The caller (llm_fallback)
        catches broad Exception and treats it as "no observation", so
        transient API issues never crash the upload flow.
        """
        ...


class GeminiProvider:
    """Production provider: wraps the existing llm_client.call_gemini."""

    def __init__(self, use_pro_model: bool = False):
        # Imported lazily so tests can run without google-genai installed
        # and without GEMINI_API_KEY being set.
        from llm_client import call_gemini, MODEL_FLASH, MODEL_PRO

        self._call = call_gemini
        self._use_pro = use_pro_model
        self.name = MODEL_PRO if use_pro_model else MODEL_FLASH

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        span_name: str = "llm_fallback",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self._call(
            prompt=prompt,
            span_name=span_name,
            metadata=metadata,
            temperature=temperature,
            use_pro_model=self._use_pro,
        )
