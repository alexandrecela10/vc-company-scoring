"""Entity resolver: map an uploaded document to a company_id.

Called by the Upload UI (step 10) AFTER preprocessing and BEFORE extraction.
The UI takes the resolver's top candidate (when confident) or presents the
top-N choices for analyst confirmation.

Signals (stacked, clamped to 1.0):
    +0.60  exact alias match (lowercased alias appears as whole word in
           filename or first-3 chunks)
    +0.50  exact company.name match (same rule, with suffixes stripped)
    +0.30  domain match (domain extracted from chunks matches company website
           or a domain-type alias)
    +0.25  filename token overlap ratio (tokens shared with company name)
    +0.25  fuzzy first-slide match (rapidfuzz token_set_ratio >= 80)

Design choices:
  - In-memory indices built once from DB rows. 20-2000 companies = trivial.
  - Case-insensitive everywhere; whole-word boundaries enforce precision.
  - Suffix stripping ("Inc", "Ltd", "Technologies") so "Tabby Technologies Ltd"
    in the deck still matches "Tabby" in the DB.
  - Stacked signals: if multiple signals fire, confidence compounds. No
    signal alone usually crosses the 0.85 auto-accept bar; that's intentional.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

from pipeline.preprocessors.base import Chunk

# --- Tunables --------------------------------------------------------------
# These are the scoring weights documented at the top. Kept as module constants
# so the test suite can reason about them and future tweaks are grep-able.
W_ALIAS_EXACT = 0.60
W_NAME_EXACT = 0.50
W_DOMAIN = 0.30
W_FILENAME_TOKENS = 0.25
W_FUZZY_FIRST_SLIDE = 0.25
FUZZY_THRESHOLD = 80  # rapidfuzz token_set_ratio must be >= this to count

# How many leading chunks we scan for company-name hits. We look at the
# first slide + 2 more, which in practice covers the title/summary slides
# without diluting with deep-in-deck financial text.
TITLE_CHUNK_LOOKBACK = 3

# Corporate suffixes stripped before exact-name matching. "Tabby Technologies
# Inc" -> "Tabby". Order doesn't matter -- we strip any suffix found at the end.
CORPORATE_SUFFIXES = {
    "inc", "incorporated",
    "ltd", "limited",
    "llc", "l.l.c.",
    "corp", "corporation",
    "co", "company",
    "gmbh", "ag",
    "sa", "s.a.",
    "plc",
    "technologies", "tech",
    "holdings", "holding",
    "group", "ventures",
    "fz-llc", "fz",
    "pte", "pty",
}

# Very small filename-noise list. Filenames tend to have "deck", "pitch",
# "series", dates, etc. These aren't distinctive of any company.
FILENAME_NOISE = {
    "deck", "pitch", "pitchdeck", "slides",
    "series", "round", "seed", "preseed",
    "v1", "v2", "v3", "final", "draft", "confidential",
    "copy", "report", "summary",
}

# Regex for domain detection inside chunk text.
# Matches host-like tokens with a known/generic TLD. Deliberately broad so
# we catch "tabby.ai" and "example.co.uk" but deliberately word-bounded so
# we don't catch inline filenames like "report.pdf".
_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?"
    r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)"
    r"\b",
    re.IGNORECASE,
)


@dataclass
class EntityCandidate:
    """One potential company match with score + signals for transparency.

    The UI can display `signals` verbatim so the analyst sees WHY this
    candidate scored high (e.g. "filename matched 'tabby', domain matched
    tabby.ai"). Keeps the resolver debuggable.
    """
    company_id: str
    company_name: str
    score: float
    signals: List[str] = field(default_factory=list)


class EntityResolver:
    """Entity resolver built from in-memory company + alias indices."""

    def __init__(self, companies: List[Dict], aliases: List[Dict]):
        # companies: list of {id, name, website, ...}
        # aliases:   list of {id, company_id, alias, alias_type}
        # Normalise once at construction so every resolve() call is cheap.
        self._companies = companies
        self._aliases = aliases

        # Per-company, pre-computed index of things we'll compare against.
        # Keyed by company_id. Value: dict with 'name', 'name_tokens',
        # 'name_stripped', 'domains', 'aliases'.
        self._index: Dict[str, Dict] = {}
        for c in companies:
            cid = str(c["id"])
            name = (c.get("name") or "").strip()
            self._index[cid] = {
                "name": name,
                "name_lower": name.lower(),
                "name_stripped": _strip_suffixes(name),
                "name_tokens": _tokenize_company_name(name),
                "website_domain": _extract_domain_from_url(c.get("website")),
                "aliases": [],        # filled below
                "domain_aliases": [], # filled below
            }
        for a in aliases:
            cid = str(a["company_id"])
            if cid not in self._index:
                continue  # orphan alias; skip
            alias_val = (a.get("alias") or "").strip().lower()
            if not alias_val:
                continue
            if a.get("alias_type") == "domain":
                self._index[cid]["domain_aliases"].append(alias_val)
            else:
                self._index[cid]["aliases"].append(alias_val)

    # --- construction sugar --------------------------------------------

    @classmethod
    def from_db(cls) -> "EntityResolver":
        """Build a resolver from the live DB (cheap, done per upload)."""
        import db  # local import so tests can stub db without side effects
        return cls(db.get_all_companies(), db.get_all_company_aliases())

    # --- public API -----------------------------------------------------

    def resolve(
        self,
        filename: str,
        chunks: List[Chunk],
        min_score: float = 0.4,
        max_results: int = 5,
    ) -> List[EntityCandidate]:
        """Return up to `max_results` candidates with score >= min_score.

        Sorted by score desc, then company_name asc (stable).
        Empty list = no confident match; caller should prompt the analyst.
        """
        # --- Pre-compute inputs once (not per-company) --------------
        filename_lower = filename.lower()
        filename_tokens = _tokenize_filename(filename)
        head_text = _head_chunk_text(chunks, TITLE_CHUNK_LOOKBACK)
        head_text_lower = head_text.lower()
        chunk_domains = _extract_domains(head_text)  # set of lowercased domains

        # --- Score every company -------------------------------------
        candidates: List[EntityCandidate] = []
        for cid, idx in self._index.items():
            score = 0.0
            signals: List[str] = []

            # 1. Exact alias match (legal name, DBA, former name, acronym).
            for alias in idx["aliases"]:
                if _whole_word_in(alias, filename_lower) or _whole_word_in(alias, head_text_lower):
                    score += W_ALIAS_EXACT
                    signals.append(f"alias:{alias}")
                    break  # one alias hit is enough; don't double-count

            # 2. Exact company.name match (suffix-stripped).
            name_stripped = idx["name_stripped"].lower()
            if name_stripped and (
                _whole_word_in(name_stripped, filename_lower)
                or _whole_word_in(name_stripped, head_text_lower)
            ):
                score += W_NAME_EXACT
                signals.append(f"name:{name_stripped}")

            # 3. Domain match (website or domain alias).
            all_domains = set(idx["domain_aliases"])
            if idx["website_domain"]:
                all_domains.add(idx["website_domain"])
            domain_hit = all_domains & chunk_domains
            if domain_hit:
                score += W_DOMAIN
                # Show which specific domain matched (helpful for debugging).
                signals.append(f"domain:{next(iter(domain_hit))}")

            # 4. Filename token overlap.
            overlap_ratio = _token_overlap_ratio(filename_tokens, idx["name_tokens"])
            if overlap_ratio > 0:
                contribution = W_FILENAME_TOKENS * overlap_ratio
                score += contribution
                signals.append(f"filename_tokens:{overlap_ratio:.2f}")

            # 5. Fuzzy match on first-slide text.
            # token_set_ratio handles reordered / extra words well:
            # "Tabby - Series A Pitch" vs "Tabby" still scores ~90.
            if head_text and idx["name_stripped"]:
                ratio = fuzz.token_set_ratio(
                    head_text_lower, idx["name_stripped"].lower()
                )
                if ratio >= FUZZY_THRESHOLD:
                    contribution = W_FUZZY_FIRST_SLIDE * (ratio / 100.0)
                    score += contribution
                    signals.append(f"fuzzy:{ratio}")

            if score >= min_score:
                candidates.append(
                    EntityCandidate(
                        company_id=cid,
                        company_name=idx["name"],
                        score=min(score, 1.0),  # clamp so UI never sees >1
                        signals=signals,
                    )
                )

        # Stable sort: score desc, then name asc.
        candidates.sort(key=lambda c: (-c.score, c.company_name.lower()))
        return candidates[:max_results]


# ---------------------------------------------------------------------------
# Helpers -- module-level (pure functions) so tests can hit them directly.
# ---------------------------------------------------------------------------

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokenize_filename(filename: str) -> List[str]:
    """Lowercased tokens from a filename, minus extension + noise words.

    'tabby_series_c_deck_2024.pdf' -> ['tabby', '2024']
    (Noise like 'series','deck' is stripped; year kept as it's distinctive.)
    """
    # Strip extension -- keep everything before the last '.'.
    stem = filename.rsplit(".", 1)[0]
    raw = [t for t in _WORD_SPLIT_RE.split(stem.lower()) if t]
    return [t for t in raw if len(t) >= 3 and t not in FILENAME_NOISE]


def _tokenize_company_name(name: str) -> List[str]:
    """Lowercased distinctive tokens from a company name (no corp suffixes)."""
    raw = [t for t in _WORD_SPLIT_RE.split(name.lower()) if t]
    return [t for t in raw if t not in CORPORATE_SUFFIXES and len(t) >= 2]


def _strip_suffixes(name: str) -> str:
    """Remove trailing corporate suffixes: 'Tabby Technologies Inc' -> 'Tabby'."""
    tokens = name.split()
    # Pop suffix tokens from the end until we hit a non-suffix.
    while tokens and tokens[-1].lower().rstrip(",.") in CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens).strip()


def _extract_domain_from_url(url: Optional[str]) -> Optional[str]:
    """Return bare hostname (no scheme, no www, no path). None on garbage."""
    if not url:
        return None
    from urllib.parse import urlparse
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower()
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _extract_domains(text: str) -> set[str]:
    """Find all domain-like tokens in a text blob. Lowercased set."""
    out = set()
    for m in _DOMAIN_RE.finditer(text):
        host = m.group(1).lower()
        if host.startswith("www."):
            host = host[4:]
        # Filter out obvious non-domains: 'file.pdf', 'version.1.0'.
        # We accept only hosts where the TLD looks plausible (2+ alpha chars).
        tld = host.rsplit(".", 1)[-1]
        if tld.isalpha() and len(tld) >= 2:
            out.add(host)
    return out


def _head_chunk_text(chunks: Iterable[Chunk], n: int) -> str:
    """Concatenate text of the first `n` chunks (by ordinal)."""
    sorted_chunks = sorted(chunks, key=lambda c: c.ordinal)[:n]
    return "\n".join(c.text for c in sorted_chunks)


def _whole_word_in(needle: str, haystack: str) -> bool:
    """Whole-word, case-insensitive substring match (inputs already lowercase).

    Matches at word boundaries so 'tabby' doesn't match 'tabbycat' and
    'ai' doesn't match 'aim'. An empty needle is always False.
    """
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _token_overlap_ratio(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Share of tokens_b found in tokens_a. Returns 0.0-1.0.

    Asymmetric by design: we ask "how much of the company name's distinctive
    tokens are in the filename?". A company named "Tabby" with filename
    'tabby_deck.pdf' scores 1.0 even though the filename has extra tokens.
    """
    if not tokens_b:
        return 0.0
    set_a = set(tokens_a)
    hits = sum(1 for t in tokens_b if t in set_a)
    return hits / len(tokens_b)
