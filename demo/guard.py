"""Cost guards for the public demo.

Layer 1 (outside code): the Gemini key comes from a Google project with no
billing account, so calls past the free quota fail instead of being charged.
Layer 2 (here): a global daily cap on LLM calls, shared by every visitor,
plus file size and page limits. Past the cap the demo runs rules only.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import date
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024        # 10 MB per upload
MAX_PAGES = 30                      # pitch decks are rarely longer
SESSION_LLM_DECKS = 3               # LLM-assisted decks per visitor session
DAILY_LLM_CALLS = int(os.environ.get("DEMO_DAILY_LLM_CALLS", "200"))


class BudgetExhausted(RuntimeError):
    """Raised when the daily LLM call cap is reached."""


class DailyBudget:
    """File-backed counter that resets each day. The file lock keeps concurrent sessions honest."""

    def __init__(self, limit: int = DAILY_LLM_CALLS, path: Path | None = None):
        self.limit = limit
        self.path = path or Path(tempfile.gettempdir()) / "deck_rank_llm_budget.json"

    def _update(self, add: int) -> int:
        """Add `add` calls to today's count if it fits. Returns the new count, or -1 if refused."""
        self.path.touch(exist_ok=True)
        with open(self.path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                data = {}
            today = date.today().isoformat()
            used = data.get(today, 0)
            if used + add > self.limit:
                return -1
            f.seek(0)
            f.truncate()
            f.write(json.dumps({today: used + add}))
            return used + add

    def used(self) -> int:
        return self._update(0)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def spend(self) -> None:
        if self._update(1) < 0:
            raise BudgetExhausted("daily demo LLM limit reached")


class BudgetedProvider:
    """Wraps an LLMProvider so every call is counted against the daily budget.

    When the budget runs out, complete() raises. llm_fallback treats a raised
    call as "no observation", so the deck still gets its rules-only result.
    """

    def __init__(self, inner, budget: DailyBudget):
        self.inner, self.budget = inner, budget
        self.name = getattr(inner, "name", "llm")
        self.calls = 0

    def complete(self, prompt: str, **kwargs) -> str:
        self.budget.spend()
        self.calls += 1
        return self.inner.complete(prompt, **kwargs)


def check_upload(raw: bytes) -> str | None:
    """Return an error message if the file breaks a limit, else None."""
    if len(raw) > MAX_BYTES:
        return f"File is {len(raw) / 1e6:.1f} MB. The limit is {MAX_BYTES // 1_000_000} MB."
    if not raw.startswith(b"%PDF"):
        return "Only PDF files are supported."
    import io
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            pages = len(pdf.pages)
    except Exception:
        return "The PDF could not be opened."
    if pages > MAX_PAGES:
        return f"The deck has {pages} pages. The limit is {MAX_PAGES}."
    return None
