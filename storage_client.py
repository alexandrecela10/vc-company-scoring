"""Thin wrapper around Supabase Storage for source_document bytes.

We hit the Supabase Storage REST API directly via httpx (already a transitive
dep of supabase-py). Doing it this way means:
  - No dependency on the exact supabase-py version
  - Supports both legacy JWT keys AND new sb_secret_* keys uniformly
  - Fewer layers when debugging

Public API:
    ensure_bucket(name)           -> None   (idempotent)
    upload_pdf(content_hash, b)   -> str    (returns storage path)
    get_signed_url(path, expires) -> str
    download_pdf(path)            -> bytes

The UI (ui_upload.py) and orchestrator (pipeline/extractors/pitchdeck_v1.py)
don't know or care what's behind this file. Swap in S3 later by rewriting
just this module.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Default bucket for uploaded pitch decks and future source documents.
# Private bucket: only service-role reads allowed. We serve bytes to the
# UI via short-lived signed URLs, never public links.
BUCKET_SOURCE_DOCS = "source-documents"


def _auth_headers() -> dict:
    """Build auth headers for every Storage request.

    Supabase Storage requires BOTH:
      - Authorization: Bearer <service-role-key>
      - apikey: <service-role-key>
    The same key value goes in both. We read SUPABASE_KEY (the service-role
    secret) from .env; the caller is responsible for setting it correctly.
    """
    key = os.getenv("SUPABASE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_KEY must be set in .env for storage operations")
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }


@lru_cache(maxsize=1)
def _base_url() -> str:
    """Return the Storage REST root, e.g. https://<project>.supabase.co/storage/v1."""
    url = os.getenv("SUPABASE_URL")
    if not url:
        raise RuntimeError("SUPABASE_URL must be set in .env for storage operations")
    return url.rstrip("/") + "/storage/v1"


def ensure_bucket(name: str = BUCKET_SOURCE_DOCS) -> None:
    """Create the bucket if it doesn't exist. Safe to call on every upload.

    POST /bucket returns 409 (or 400 with 'already exists') when the bucket
    is present. We accept that path and raise anything unexpected.
    """
    resp = httpx.post(
        f"{_base_url()}/bucket",
        headers=_auth_headers(),
        json={"id": name, "name": name, "public": False},
        timeout=10.0,
    )
    if resp.status_code in (200, 201):
        logger.info("created storage bucket %s", name)
        return
    body = resp.text.lower()
    if resp.status_code == 409 or "already exists" in body or "duplicate" in body:
        return  # idempotent success
    raise RuntimeError(f"ensure_bucket({name}) failed: {resp.status_code} {resp.text}")


def _path_for_hash(content_hash: str) -> str:
    """Shard bytes into 256 subfolders by hash prefix.

    Why: a single flat folder with 10k+ objects is painful to browse in the
    Supabase UI, and some providers throttle 'list' calls at that scale.
    Prefixing by the first two hex chars gives us a clean 2-level tree.
    """
    return f"{content_hash[:2]}/{content_hash}.pdf"


def upload_pdf(content_hash: str, raw_bytes: bytes, bucket: str = BUCKET_SOURCE_DOCS) -> str:
    """Upload PDF bytes, keyed by SHA-256 content hash.

    Idempotent: `x-upsert: true` makes repeat uploads of the same hash a no-op.
    Returns the storage path (NOT a signed URL) so callers can cache it in
    source_document.origin_url -- signed URLs expire and shouldn't be stored.
    """
    ensure_bucket(bucket)
    path = _path_for_hash(content_hash)
    headers = {
        **_auth_headers(),
        "Content-Type": "application/pdf",
        "x-upsert": "true",
    }
    resp = httpx.post(
        f"{_base_url()}/object/{bucket}/{path}",
        headers=headers,
        content=raw_bytes,
        timeout=60.0,  # 50MB PDFs can take a few seconds
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"upload_pdf failed: {resp.status_code} {resp.text}")
    return path


def get_signed_url(path: str, expires_in: int = 3600, bucket: str = BUCKET_SOURCE_DOCS) -> str:
    """Return a time-limited URL for reading the object.

    Used by the UI when an analyst clicks "view original deck" -- we never
    hand out public URLs for ingested documents.
    """
    resp = httpx.post(
        f"{_base_url()}/object/sign/{bucket}/{path}",
        headers=_auth_headers(),
        json={"expiresIn": expires_in},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"get_signed_url failed: {resp.status_code} {resp.text}")
    data = resp.json()
    # Supabase returns a relative signedURL like '/object/sign/...'; prepend origin.
    signed = data.get("signedURL") or data.get("signedUrl")
    if not signed:
        raise RuntimeError(f"signed URL missing from response: {data}")
    origin = os.getenv("SUPABASE_URL", "").rstrip("/")
    return signed if signed.startswith("http") else f"{origin}/storage/v1{signed}"


def download_pdf(path: str, bucket: str = BUCKET_SOURCE_DOCS) -> bytes:
    """Fetch bytes for a stored object. Used when re-extracting an old deck."""
    resp = httpx.get(
        f"{_base_url()}/object/{bucket}/{path}",
        headers=_auth_headers(),
        timeout=60.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"download_pdf failed: {resp.status_code} {resp.text}")
    return resp.content


def get_source_document_by_hash(content_hash: str) -> Optional[dict]:
    """Check if a source_document row already exists for this hash.

    Used by the upload UI to warn the analyst 'this deck was already ingested'
    BEFORE doing the expensive extraction work.
    """
    import db
    return db._fetchone(
        "SELECT * FROM source_document WHERE content_hash = %s",
        (content_hash,),
    )
