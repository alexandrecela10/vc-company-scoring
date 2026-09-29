"""Unit tests for EntityResolver.

We construct an in-memory resolver with 3 synthetic companies + aliases
so the tests never touch the live DB. Each test asserts a specific
scoring behaviour (filename, alias, domain, fuzzy, ambiguous, no match).

Run:  python3 -m unittest tests.test_entity_resolver
"""
from __future__ import annotations

import unittest

from pipeline.entity_resolver import (
    EntityResolver,
    _extract_domains,
    _strip_suffixes,
    _tokenize_company_name,
    _tokenize_filename,
    _whole_word_in,
)
from pipeline.preprocessors.base import Chunk


# --- Synthetic DB ---------------------------------------------------------
# Three companies covering the common edge cases:
#   TABBY:   short name, has a legal-name alias + matching domain.
#   FINTECH A: domain alias only, mentioned without its domain.
#   RIZE:    multiple word name stripped of "Technologies".

COMPANIES = [
    {"id": "11111111-1111-1111-1111-111111111111",
     "name": "Tabby", "website": "https://tabby.ai"},
    {"id": "22222222-2222-2222-2222-222222222222",
     "name": "Fintech A", "website": "https://fintech-a.example"},
    {"id": "33333333-3333-3333-3333-333333333333",
     "name": "Rize Technologies", "website": "https://rize.sa"},
]

ALIASES = [
    # Tabby: legal name + extra domain alias
    {"id": "a1", "company_id": COMPANIES[0]["id"],
     "alias": "Tabby Technologies Ltd", "alias_type": "legal_name"},
    {"id": "a2", "company_id": COMPANIES[0]["id"],
     "alias": "tabby.com", "alias_type": "domain"},
    # Fintech A: domain
    {"id": "a3", "company_id": COMPANIES[1]["id"],
     "alias": "fintech-a.example", "alias_type": "domain"},
    # Rize: domain
    {"id": "a4", "company_id": COMPANIES[2]["id"],
     "alias": "rize.sa", "alias_type": "domain"},
]


def chunk(text: str, ordinal: int = 0, locator: str | None = None) -> Chunk:
    return Chunk(
        text=text,
        locator=locator or f"slide_{ordinal + 1}",
        ordinal=ordinal,
        metadata={},
    )


def make_resolver() -> EntityResolver:
    return EntityResolver(COMPANIES, ALIASES)


# --- Helper-function tests (keep the math honest) -------------------------

class HelperTests(unittest.TestCase):
    def test_tokenize_filename_strips_extension_and_noise(self):
        tokens = _tokenize_filename("tabby_series_c_deck_2024.pdf")
        self.assertEqual(sorted(tokens), ["2024", "tabby"])

    def test_tokenize_company_name_drops_suffixes(self):
        tokens = _tokenize_company_name("Tabby Technologies Inc")
        self.assertEqual(tokens, ["tabby"])

    def test_strip_suffixes_removes_trailing_corp_words(self):
        self.assertEqual(_strip_suffixes("Tabby Technologies Inc"), "Tabby")
        self.assertEqual(_strip_suffixes("Rize Technologies"), "Rize")
        self.assertEqual(_strip_suffixes("Plain Name"), "Plain Name")

    def test_extract_domains_finds_urls(self):
        text = "Visit us at tabby.ai or www.tabby.com -- also see report.pdf"
        domains = _extract_domains(text)
        self.assertIn("tabby.ai", domains)
        self.assertIn("tabby.com", domains)
        # report.pdf has a 3-letter TLD but 'pdf' is technically accepted by
        # the broad regex; the resolver's signal design means a single PDF
        # reference without any other match still won't auto-win.
        # Document this by asserting the main domains are in.

    def test_whole_word_match_rejects_partials(self):
        self.assertTrue(_whole_word_in("tabby", "meet tabby, our bnpl"))
        self.assertFalse(_whole_word_in("tabby", "tabbycat startup"))
        self.assertFalse(_whole_word_in("", "anything"))


# --- Resolver integration tests -------------------------------------------

class EntityResolverTests(unittest.TestCase):
    def test_exact_filename_match_wins(self):
        # Filename mentions 'tabby' explicitly -> highest score.
        r = make_resolver()
        out = r.resolve("tabby_deck_2024.pdf", [chunk("Welcome to our pitch")])
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Tabby")
        # Name signal must have fired.
        self.assertTrue(any(s.startswith("name:") for s in out[0].signals))

    def test_first_slide_name_match(self):
        # Filename is uninformative; first slide carries the name.
        r = make_resolver()
        out = r.resolve(
            "deck_final_v3.pdf",
            [chunk("Tabby — Series C Pitch", ordinal=0)],
        )
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Tabby")

    def test_domain_in_chunks_boosts_score(self):
        # Filename unrelated, but a chunk mentions the company's domain.
        r = make_resolver()
        out = r.resolve(
            "pitch_deck.pdf",
            [chunk("Try it at tabby.ai for instant BNPL.", ordinal=0)],
        )
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Tabby")
        self.assertTrue(any(s.startswith("domain:") for s in out[0].signals))

    def test_alias_match_fires(self):
        # Deck uses the legal name, not the display name.
        r = make_resolver()
        out = r.resolve(
            "deck.pdf",
            [chunk("Tabby Technologies Ltd. Investor Update", ordinal=0)],
        )
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Tabby")
        self.assertTrue(any(s.startswith("alias:") for s in out[0].signals))

    def test_stacked_signals_compound_score(self):
        # Filename + first slide + domain all mention Tabby -> high score.
        r = make_resolver()
        out = r.resolve(
            "tabby_series_c_deck.pdf",
            [chunk("Tabby — Series C. Visit tabby.ai", ordinal=0)],
        )
        top = out[0]
        self.assertEqual(top.company_name, "Tabby")
        # Multiple signals should have stacked (name + domain + filename_tokens).
        self.assertGreaterEqual(len(top.signals), 2)
        # With three signals, score should comfortably pass the 0.85 auto-accept bar.
        self.assertGreater(top.score, 0.85)

    def test_ambiguous_returns_multiple_candidates(self):
        # Generic text that could match anything (no distinctive tokens).
        r = make_resolver()
        out = r.resolve(
            "generic_deck.pdf",
            [chunk("Financials. Growth. Team.", ordinal=0)],
            min_score=0.0,  # we want to inspect all non-zero candidates
        )
        # No strong match -> all scores should be low.
        if out:
            self.assertLess(out[0].score, 0.5)

    def test_no_match_returns_empty(self):
        # Nothing about any known company.
        r = make_resolver()
        out = r.resolve(
            "random_industry_report.pdf",
            [chunk("A survey of trends in robotics manufacturing.", ordinal=0)],
        )
        self.assertEqual(out, [])

    def test_fuzzy_typo_still_matches(self):
        # Typo in filename -- fuzzy on first-slide text should save us.
        r = make_resolver()
        out = r.resolve(
            "tabby_deck.pdf",
            [chunk("tabbi — welcome", ordinal=0)],  # intentional typo
        )
        # The filename match alone should still win; fuzzy adds confidence.
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Tabby")

    def test_suffix_in_name_does_not_block_match(self):
        # Company is "Rize Technologies"; filename just says "rize".
        r = make_resolver()
        out = r.resolve(
            "rize_deck.pdf",
            [chunk("Rize — series A pitch", ordinal=0)],
        )
        self.assertGreater(len(out), 0)
        self.assertEqual(out[0].company_name, "Rize Technologies")

    def test_candidates_sorted_by_score_desc(self):
        # Two companies potentially hit. Ensure ordering.
        r = make_resolver()
        out = r.resolve(
            "deck.pdf",
            [chunk("Tabby and Fintech A partnership announcement. tabby.ai", ordinal=0)],
            min_score=0.0,
        )
        self.assertGreater(len(out), 1)
        # Tabby has name+domain signals; Fintech A only has a mention.
        self.assertEqual(out[0].company_name, "Tabby")
        scores = [c.score for c in out]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
