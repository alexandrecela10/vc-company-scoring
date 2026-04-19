"""
Grounding Module — Deterministic validation for AI outputs.

This module provides 100% deterministic validation functions that verify
AI-generated claims against source data using exact string matching.
No LLM calls here — pure Python string operations.

Key principle: Every AI output must have PROOF from the source text.
If we can't find an exact match, the claim is flagged as "ungrounded".

Functions:
- find_exact_match: Find exact substring in source, return match + context
- validate_company_name: Verify company name appears in source
- validate_website_domain: Check if website domain matches company name
- validate_claim: Generic claim validation with exact match proof
- build_grounded_evidence: Create evidence object with deterministic proof
"""

import re
import logging
import requests
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# HTTP request settings for website verification
HTTP_TIMEOUT = 10  # seconds
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


@dataclass
class GroundedEvidence:
    """
    Proof that an AI claim is grounded in source data.
    
    This is the core data structure for absolute grounding.
    Every field extracted by AI should have one of these attached.
    
    match_type:
    - "exact": The exact string was found in the source
    - "partial": A significant portion was found (e.g., company name without suffix)
    - "none": No match found — this claim is UNGROUNDED
    
    The snippet fields show the ACTUAL text from the source,
    not what the AI claimed. This is the hard proof.
    """
    claim: str                          # What the AI claimed (e.g., "Byanat AI")
    claim_field: str                    # Which field this is (e.g., "company_name", "website")
    source_url: str                     # URL where we looked for evidence
    source_text: str                    # Full source text we searched in
    
    # Deterministic match results
    match_type: str = "none"            # "exact", "partial", "none"
    matched_text: str = ""              # The EXACT text found in source
    match_start: int = -1               # Character position where match starts
    match_end: int = -1                 # Character position where match ends
    
    # Context around the match (for display)
    context_before: str = ""            # 50 chars before the match
    context_after: str = ""             # 50 chars after the match
    
    # Validation result
    is_grounded: bool = False           # True only if match_type != "none"
    confidence: float = 0.0             # 1.0 for exact, 0.7 for partial, 0.0 for none
    validation_method: str = ""         # How we validated (e.g., "exact_substring_match")

    def to_dict(self) -> Dict:
        return {
            "claim": self.claim,
            "claim_field": self.claim_field,
            "source_url": self.source_url,
            "match_type": self.match_type,
            "matched_text": self.matched_text,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "is_grounded": self.is_grounded,
            "confidence": self.confidence,
            "validation_method": self.validation_method,
        }

    def get_proof_snippet(self) -> str:
        """Return a formatted proof snippet showing the match in context."""
        if not self.is_grounded:
            return f"❌ NOT FOUND: '{self.claim}' not found in source"
        return f"✅ FOUND: ...{self.context_before}[{self.matched_text}]{self.context_after}..."


def find_exact_match(
    needle: str,
    haystack: str,
    case_sensitive: bool = False,
    context_chars: int = 50,
) -> Tuple[bool, str, int, int, str, str]:
    """
    Find exact substring match in text.
    
    Returns:
        (found, matched_text, start_pos, end_pos, context_before, context_after)
    
    This is the core deterministic function — no AI, just string search.
    """
    if not needle or not haystack:
        return (False, "", -1, -1, "", "")
    
    search_needle = needle if case_sensitive else needle.lower()
    search_haystack = haystack if case_sensitive else haystack.lower()
    
    pos = search_haystack.find(search_needle)
    
    if pos == -1:
        return (False, "", -1, -1, "", "")
    
    # Get the actual matched text (preserving original case)
    matched_text = haystack[pos:pos + len(needle)]
    
    # Get context
    context_start = max(0, pos - context_chars)
    context_end = min(len(haystack), pos + len(needle) + context_chars)
    
    context_before = haystack[context_start:pos].strip()
    context_after = haystack[pos + len(needle):context_end].strip()
    
    return (True, matched_text, pos, pos + len(needle), context_before, context_after)


def validate_company_name(
    company_name: str,
    source_text: str,
    source_url: str = "",
) -> GroundedEvidence:
    """
    Validate that a company name appears in the source text.
    
    Tries multiple matching strategies:
    1. Exact match (case-insensitive)
    2. First word match (for "Company AI" -> "Company")
    3. Without common suffixes (AI, Tech, Inc, etc.)
    
    Returns GroundedEvidence with deterministic proof.
    """
    evidence = GroundedEvidence(
        claim=company_name,
        claim_field="company_name",
        source_url=source_url,
        source_text=source_text[:500] if source_text else "",  # Truncate for storage
        validation_method="exact_substring_match",
    )
    
    if not company_name or not source_text:
        return evidence
    
    # Strategy 1: Exact match
    found, matched, start, end, before, after = find_exact_match(company_name, source_text)
    if found:
        evidence.match_type = "exact"
        evidence.matched_text = matched
        evidence.match_start = start
        evidence.match_end = end
        evidence.context_before = before
        evidence.context_after = after
        evidence.is_grounded = True
        evidence.confidence = 1.0
        return evidence
    
    # Strategy 2: First word match (for multi-word names)
    words = company_name.split()
    if len(words) > 1:
        first_word = words[0]
        if len(first_word) >= 3:  # Avoid matching short words like "AI"
            found, matched, start, end, before, after = find_exact_match(first_word, source_text)
            if found:
                evidence.match_type = "partial"
                evidence.matched_text = matched
                evidence.match_start = start
                evidence.match_end = end
                evidence.context_before = before
                evidence.context_after = after
                evidence.is_grounded = True
                evidence.confidence = 0.7
                evidence.validation_method = "first_word_match"
                return evidence
    
    # Strategy 3: Without common suffixes
    suffixes = [" ai", " tech", " inc", " ltd", " llc", " co", " labs", " io"]
    clean_name = company_name.lower()
    for suffix in suffixes:
        if clean_name.endswith(suffix):
            clean_name = clean_name[:-len(suffix)].strip()
            break
    
    if clean_name and len(clean_name) >= 3 and clean_name != company_name.lower():
        found, matched, start, end, before, after = find_exact_match(clean_name, source_text)
        if found:
            evidence.match_type = "partial"
            evidence.matched_text = matched
            evidence.match_start = start
            evidence.match_end = end
            evidence.context_before = before
            evidence.context_after = after
            evidence.is_grounded = True
            evidence.confidence = 0.6
            evidence.validation_method = "suffix_removed_match"
            return evidence
    
    # No match found
    evidence.match_type = "none"
    evidence.is_grounded = False
    evidence.confidence = 0.0
    return evidence


def validate_website_domain(
    claimed_website: str,
    company_name: str,
    source_text: str = "",
    source_url: str = "",
) -> GroundedEvidence:
    """
    Validate that a website URL is plausibly correct for a company.
    
    Checks:
    1. Domain contains company name (or variation)
    2. Website URL appears in source text
    3. Domain is not a generic site (linkedin, crunchbase, etc.)
    
    Returns GroundedEvidence with deterministic proof.
    """
    evidence = GroundedEvidence(
        claim=claimed_website,
        claim_field="website",
        source_url=source_url,
        source_text=source_text[:500] if source_text else "",
        validation_method="domain_validation",
    )
    
    if not claimed_website or claimed_website == "Not Found":
        evidence.match_type = "none"
        return evidence
    
    # Parse the domain
    try:
        parsed = urlparse(claimed_website)
        domain = parsed.netloc.lower().replace("www.", "")
    except Exception:
        evidence.match_type = "none"
        return evidence
    
    # Check for generic domains (these are NOT company websites)
    generic_domains = [
        "linkedin.com", "crunchbase.com", "twitter.com", "facebook.com",
        "instagram.com", "youtube.com", "medium.com", "github.com",
        "techcrunch.com", "forbes.com", "bloomberg.com", "reuters.com",
        "wamda.com", "magnitt.com", "zawya.com",
    ]
    if any(generic in domain for generic in generic_domains):
        evidence.match_type = "none"
        evidence.is_grounded = False
        evidence.confidence = 0.0
        evidence.validation_method = "rejected_generic_domain"
        return evidence
    
    # Check 1: Does the domain contain the company name?
    company_clean = re.sub(r'[^a-z0-9]', '', company_name.lower())
    domain_clean = re.sub(r'[^a-z0-9]', '', domain)
    
    if company_clean and len(company_clean) >= 3:
        # Check if company name is in domain
        if company_clean in domain_clean:
            evidence.match_type = "exact"
            evidence.matched_text = domain
            evidence.is_grounded = True
            evidence.confidence = 0.9
            evidence.validation_method = "company_name_in_domain"
            return evidence
        
        # Check first word of company name
        first_word = re.sub(r'[^a-z0-9]', '', company_name.split()[0].lower())
        if first_word and len(first_word) >= 3 and first_word in domain_clean:
            evidence.match_type = "partial"
            evidence.matched_text = domain
            evidence.is_grounded = True
            evidence.confidence = 0.7
            evidence.validation_method = "company_first_word_in_domain"
            return evidence
    
    # Check 2: Does the website URL appear in the source text?
    if source_text:
        # Try full URL
        found, matched, start, end, before, after = find_exact_match(claimed_website, source_text)
        if found:
            evidence.match_type = "exact"
            evidence.matched_text = matched
            evidence.match_start = start
            evidence.match_end = end
            evidence.context_before = before
            evidence.context_after = after
            evidence.is_grounded = True
            evidence.confidence = 1.0
            evidence.validation_method = "url_found_in_source"
            return evidence
        
        # Try just the domain
        found, matched, start, end, before, after = find_exact_match(domain, source_text)
        if found:
            evidence.match_type = "partial"
            evidence.matched_text = matched
            evidence.match_start = start
            evidence.match_end = end
            evidence.context_before = before
            evidence.context_after = after
            evidence.is_grounded = True
            evidence.confidence = 0.8
            evidence.validation_method = "domain_found_in_source"
            return evidence
    
    # No validation possible
    evidence.match_type = "none"
    evidence.is_grounded = False
    evidence.confidence = 0.0
    evidence.validation_method = "no_validation_possible"
    return evidence


def validate_claim(
    claim: str,
    claim_field: str,
    source_text: str,
    source_url: str = "",
) -> GroundedEvidence:
    """
    Generic claim validation — find exact match of claim in source.
    
    Use this for any AI-generated text that should appear in the source:
    - Descriptions
    - Founder names
    - Funding amounts
    - Locations
    - etc.
    """
    evidence = GroundedEvidence(
        claim=claim,
        claim_field=claim_field,
        source_url=source_url,
        source_text=source_text[:500] if source_text else "",
        validation_method="exact_substring_match",
    )
    
    if not claim or not source_text or claim in ["Not Found", "N/A", ""]:
        evidence.match_type = "none"
        return evidence
    
    # Try exact match
    found, matched, start, end, before, after = find_exact_match(claim, source_text)
    if found:
        evidence.match_type = "exact"
        evidence.matched_text = matched
        evidence.match_start = start
        evidence.match_end = end
        evidence.context_before = before
        evidence.context_after = after
        evidence.is_grounded = True
        evidence.confidence = 1.0
        return evidence
    
    # Try matching key phrases (for longer claims)
    if len(claim) > 20:
        # Extract key phrases (words 4+ chars)
        words = [w for w in claim.split() if len(w) >= 4]
        matches_found = 0
        for word in words[:5]:  # Check first 5 significant words
            if word.lower() in source_text.lower():
                matches_found += 1
        
        if matches_found >= 2:
            evidence.match_type = "partial"
            evidence.matched_text = f"{matches_found} key words found"
            evidence.is_grounded = True
            evidence.confidence = min(0.5 + (matches_found * 0.1), 0.8)
            evidence.validation_method = "key_phrase_match"
            return evidence
    
    # No match
    evidence.match_type = "none"
    evidence.is_grounded = False
    evidence.confidence = 0.0
    return evidence


def validate_all_fields(
    extracted_data: Dict,
    source_text: str,
    source_url: str = "",
) -> Dict[str, GroundedEvidence]:
    """
    Validate all fields in an extracted company record.
    
    Returns a dict mapping field names to their GroundedEvidence.
    """
    evidence_map = {}
    
    # Company name — special handling
    if "name" in extracted_data:
        evidence_map["name"] = validate_company_name(
            extracted_data["name"], source_text, source_url
        )
    
    # Website — special handling
    if "website" in extracted_data:
        evidence_map["website"] = validate_website_domain(
            extracted_data["website"],
            extracted_data.get("name", ""),
            source_text,
            source_url,
        )
    
    # All other fields — generic validation
    for field_name in ["description", "location", "sector", "funding_stage", "funding_amount"]:
        if field_name in extracted_data:
            evidence_map[field_name] = validate_claim(
                str(extracted_data[field_name]),
                field_name,
                source_text,
                source_url,
            )
    
    # Founders — validate each name
    if "founders" in extracted_data and isinstance(extracted_data["founders"], list):
        founder_evidences = []
        for founder in extracted_data["founders"]:
            ev = validate_claim(founder, "founder_name", source_text, source_url)
            founder_evidences.append(ev)
        # Aggregate: grounded if at least one founder is found
        if founder_evidences:
            best = max(founder_evidences, key=lambda e: e.confidence)
            evidence_map["founders"] = best
    
    return evidence_map


def compute_grounding_score(evidence_map: Dict[str, GroundedEvidence]) -> float:
    """
    Compute overall grounding score for a company.
    
    Returns 0.0 to 1.0 based on how many fields are grounded.
    Critical fields (name, website) are weighted higher.
    """
    if not evidence_map:
        return 0.0
    
    weights = {
        "name": 3.0,        # Critical — must be grounded
        "website": 2.0,     # Important — often wrong
        "description": 1.0,
        "location": 1.0,
        "sector": 1.0,
        "funding_stage": 0.5,
        "funding_amount": 0.5,
        "founders": 1.0,
    }
    
    total_weight = 0.0
    weighted_score = 0.0
    
    for field, evidence in evidence_map.items():
        weight = weights.get(field, 1.0)
        total_weight += weight
        weighted_score += weight * evidence.confidence
    
    return weighted_score / total_weight if total_weight > 0 else 0.0


# ---------------------------------------------------------------------------
# Website Verification via HTTP Fetch
# ---------------------------------------------------------------------------
# This actually visits the website to verify it exists and contains the
# company name. Prevents hallucinated websites from passing validation.

def verify_website_exists(url: str) -> Tuple[bool, int, str]:
    """
    Actually fetch the website to verify it exists.
    
    Returns:
        (exists: bool, status_code: int, error_message: str)
    
    Why this matters:
    - LLM might hallucinate a plausible-looking URL like "companyname.com"
    - Without fetching, we can't know if it's real
    - This adds ~1-2 seconds per company but catches fake websites
    """
    if not url or url == "Not Found":
        return (False, 0, "No URL provided")
    
    # Ensure URL has scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        response = requests.head(
            url,
            timeout=HTTP_TIMEOUT,
            headers=HTTP_HEADERS,
            allow_redirects=True,
        )
        # 2xx and 3xx are success
        if response.status_code < 400:
            return (True, response.status_code, "")
        else:
            return (False, response.status_code, f"HTTP {response.status_code}")
    except requests.exceptions.Timeout:
        return (False, 0, "Timeout")
    except requests.exceptions.ConnectionError:
        return (False, 0, "Connection failed - site may not exist")
    except requests.exceptions.SSLError:
        return (False, 0, "SSL error")
    except Exception as e:
        return (False, 0, str(e)[:100])


def verify_website_contains_company(
    url: str,
    company_name: str,
) -> Tuple[bool, float, str, str]:
    """
    Fetch website content and verify it mentions the company name.
    
    This is the "human way" of clicking a link and checking if it's right.
    
    Returns:
        (verified: bool, confidence: float, matched_text: str, page_title: str)
    
    Why this matters:
    - A website might exist but be unrelated to the company
    - e.g., "acme.com" exists but isn't "Acme AI Startup"
    - We fetch the page and look for the company name in content
    """
    if not url or url == "Not Found":
        return (False, 0.0, "", "")
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers=HTTP_HEADERS,
            allow_redirects=True,
        )
        
        if response.status_code >= 400:
            return (False, 0.0, "", f"HTTP {response.status_code}")
        
        # Get page content (limit to first 50KB to avoid huge pages)
        content = response.text[:50000].lower()
        
        # Extract title if present
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', content, re.IGNORECASE)
        page_title = title_match.group(1).strip() if title_match else ""
        
        # Clean company name for matching
        company_clean = company_name.lower().strip()
        company_words = [w for w in company_clean.split() if len(w) >= 3]
        
        # Check 1: Full company name in content
        if company_clean in content:
            return (True, 1.0, company_clean, page_title)
        
        # Check 2: Full company name in title
        if company_clean in page_title.lower():
            return (True, 0.95, company_clean, page_title)
        
        # Check 3: First significant word in title or content
        if company_words:
            first_word = company_words[0]
            if first_word in page_title.lower():
                return (True, 0.8, first_word, page_title)
            if first_word in content:
                return (True, 0.7, first_word, page_title)
        
        # Check 4: Multiple words match
        matches = sum(1 for w in company_words if w in content)
        if matches >= 2:
            return (True, 0.6, f"{matches} words matched", page_title)
        
        # No match - website exists but doesn't seem related
        return (False, 0.0, "", page_title)
        
    except requests.exceptions.Timeout:
        return (False, 0.0, "", "Timeout")
    except requests.exceptions.ConnectionError:
        return (False, 0.0, "", "Connection failed")
    except Exception as e:
        return (False, 0.0, "", str(e)[:50])


def verify_source_content(
    source_url: str,
    expected_text: str,
) -> Tuple[bool, float, str]:
    """
    Fetch the actual source URL and verify the expected text is present.
    
    This is the "click the link and see" verification.
    
    Returns:
        (verified: bool, confidence: float, matched_snippet: str)
    """
    if not source_url or not expected_text:
        return (False, 0.0, "")
    
    try:
        response = requests.get(
            source_url,
            timeout=HTTP_TIMEOUT,
            headers=HTTP_HEADERS,
            allow_redirects=True,
        )
        
        if response.status_code >= 400:
            return (False, 0.0, f"HTTP {response.status_code}")
        
        content = response.text[:100000].lower()  # First 100KB
        expected_lower = expected_text.lower().strip()
        
        # Exact match
        if expected_lower in content:
            # Find position and extract context
            pos = content.find(expected_lower)
            start = max(0, pos - 30)
            end = min(len(content), pos + len(expected_lower) + 30)
            snippet = content[start:end]
            return (True, 1.0, snippet)
        
        # Partial match - check key words
        words = [w for w in expected_lower.split() if len(w) >= 4]
        matches = sum(1 for w in words if w in content)
        if words and matches >= len(words) * 0.5:
            return (True, 0.6, f"{matches}/{len(words)} key words found")
        
        return (False, 0.0, "Text not found in source")
        
    except Exception as e:
        return (False, 0.0, str(e)[:50])


@dataclass
class WebsiteVerification:
    """Result of website verification via HTTP fetch."""
    url: str
    exists: bool = False
    status_code: int = 0
    contains_company: bool = False
    company_match_confidence: float = 0.0
    matched_text: str = ""
    page_title: str = ""
    error: str = ""
    verified_at: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "url": self.url,
            "exists": self.exists,
            "status_code": self.status_code,
            "contains_company": self.contains_company,
            "company_match_confidence": self.company_match_confidence,
            "matched_text": self.matched_text,
            "page_title": self.page_title,
            "error": self.error,
            "verified_at": self.verified_at,
        }


def full_website_verification(
    url: str,
    company_name: str,
) -> WebsiteVerification:
    """
    Complete website verification: exists + contains company name.
    
    This is the definitive check that prevents hallucinated websites.
    """
    result = WebsiteVerification(
        url=url,
        verified_at=datetime.now().isoformat(),
    )
    
    # Step 1: Check if website exists
    exists, status_code, error = verify_website_exists(url)
    result.exists = exists
    result.status_code = status_code
    
    if not exists:
        result.error = error
        return result
    
    # Step 2: Check if website mentions company
    contains, confidence, matched, title = verify_website_contains_company(url, company_name)
    result.contains_company = contains
    result.company_match_confidence = confidence
    result.matched_text = matched
    result.page_title = title
    
    if not contains:
        result.error = f"Website exists but doesn't mention '{company_name}'"
    
    return result


# ---------------------------------------------------------------------------
# Source Date Filtering
# ---------------------------------------------------------------------------

def parse_source_date(date_str: str) -> Optional[datetime]:
    """
    Parse various date formats from source metadata.
    
    Tavily and other sources return dates in different formats.
    """
    if not date_str:
        return None
    
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%d %b %Y",
        "%B %d, %Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str[:19], fmt)
        except ValueError:
            continue
    
    return None


def is_source_recent(
    date_str: str,
    max_age_days: int = 14,
) -> Tuple[bool, Optional[datetime], int]:
    """
    Check if a source is within the allowed age.
    
    Returns:
        (is_recent: bool, parsed_date: datetime or None, age_days: int)
    
    Default: 14 days (2 weeks)
    """
    parsed = parse_source_date(date_str)
    
    if not parsed:
        # Can't determine date - assume it's okay but flag it
        return (True, None, -1)
    
    age = datetime.now() - parsed
    age_days = age.days
    
    return (age_days <= max_age_days, parsed, age_days)


def filter_sources_by_date(
    sources: List[Dict],
    max_age_days: int = 14,
    date_field: str = "published_date",
) -> Tuple[List[Dict], List[Dict]]:
    """
    Filter sources to only include recent ones.
    
    Returns:
        (recent_sources, filtered_out_sources)
    """
    recent = []
    old = []
    
    for source in sources:
        date_str = source.get(date_field, "")
        is_recent, parsed, age = is_source_recent(date_str, max_age_days)
        
        # Add age metadata
        source["_parsed_date"] = parsed.isoformat() if parsed else None
        source["_age_days"] = age
        source["_is_recent"] = is_recent
        
        if is_recent:
            recent.append(source)
        else:
            old.append(source)
    
    return recent, old
