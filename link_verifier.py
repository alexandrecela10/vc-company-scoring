"""
link_verifier — deterministic check that an evidence URL really backs a quote.

Inspired by alpha_scout's grounding logic: the LLM can easily hallucinate
citations, so we independently fetch the page and check the quote appears
in the rendered text. No LLM involved.

Usage:
    ok, reason = verify(url, evidence_quote)
    # ok=True, reason='verified'                → URL reachable + quote found
    # ok=False, reason='http_404'               → URL unreachable
    # ok=False, reason='quote_not_in_page'      → URL works but quote not found
    # ok=False, reason='fetch_error: <msg>'     → transport error
"""

from __future__ import annotations

import re
import logging
from typing import Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Standard browser UA — many news/research sites block default `python-requests`.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT_SEC = 10
# If the quote has fewer than this many chars, we don't trust the match
# (too easy to false-positive on boilerplate like "The market is growing.").
_MIN_QUOTE_LEN = 20


def _normalize(text: str) -> str:
    """
    Normalize text so quote matching ignores formatting noise:
      - lowercase
      - collapse all whitespace to single spaces
      - remove spaces that appear BEFORE punctuation (HTML scrapers often
        inject `word , next` where the page visually shows `word, next`)
    """
    lowered = text.lower()
    collapsed = re.sub(r"\s+", " ", lowered)
    # Strip whitespace before punctuation: ", " / ". " / ";" / ":" / ")" / "]"
    return re.sub(r"\s+([,.;:)\]!?])", r"\1", collapsed).strip()


def _fetch_text(url: str) -> str:
    """Fetch URL and return plain text (HTML stripped). Raises on failure."""
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT_SEC,
        allow_redirects=True,
    )
    resp.raise_for_status()
    # html.parser is built-in; no lxml dependency needed
    soup = BeautifulSoup(resp.text, "html.parser")
    # Remove scripts/styles which pollute the text with JS code
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def verify(url: str, evidence_quote: str) -> Tuple[bool, str]:
    """
    Verify that `evidence_quote` appears on the page at `url`.

    Returns (ok, reason). `ok=True` only if URL is reachable AND the normalized
    quote is a substring of the normalized page text.
    """
    if not url or not evidence_quote:
        return False, "missing_input"

    if len(evidence_quote) < _MIN_QUOTE_LEN:
        # Too short — could match by accident. Fail closed.
        return False, "quote_too_short"

    try:
        page_text = _fetch_text(url)
    except requests.HTTPError as e:
        return False, f"http_{e.response.status_code}"
    except requests.RequestException as e:
        return False, f"fetch_error: {type(e).__name__}"
    except Exception as e:
        logger.exception("Unexpected error verifying %s", url)
        return False, f"fetch_error: {e}"

    if _normalize(evidence_quote) in _normalize(page_text):
        return True, "verified"
    return False, "quote_not_in_page"
