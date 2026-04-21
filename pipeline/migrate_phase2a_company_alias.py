"""Phase 2a migration: create company_alias + seed domain aliases.

Idempotent:
  - CREATE TABLE IF NOT EXISTS
  - INSERT ... ON CONFLICT DO NOTHING on (company_id, alias, alias_type)

Usage:  python3 -m pipeline.migrate_phase2a_company_alias
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_conn  # noqa: E402

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS company_alias (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id  UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_type  TEXT NOT NULL
        CHECK (alias_type IN ('legal_name','domain','dba','former_name','acronym')),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (company_id, alias, alias_type)
);

CREATE INDEX IF NOT EXISTS idx_company_alias_lower
    ON company_alias (LOWER(alias));

CREATE INDEX IF NOT EXISTS idx_company_alias_company
    ON company_alias (company_id);

COMMENT ON TABLE company_alias IS
    'Append-only aliases for a company (legal name, domain, DBA, former name, acronym). '
    'Read by the entity resolver to match uploaded documents to the correct company_id.';
"""


def extract_domain(url: str | None) -> str | None:
    """Parse a URL and return the bare hostname without the leading 'www.'.

    Returns None when the URL is empty, malformed, or has no netloc.
    We keep only the host (no path) because we want to match things like
    'tabby.ai' appearing in a deck -- the path on company.website may be
    noise (old marketing landing page, etc.).
    """
    if not url:
        return None
    # urlparse needs a scheme to populate netloc; prepend if missing.
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def main() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        # --- Step 1: create table + indexes ---
        cur.execute(MIGRATION_SQL)
        print("[migrate] company_alias table ready")

        # --- Step 2: seed domain aliases from existing company.website ---
        cur.execute("SELECT id, name, website FROM company")
        companies = cur.fetchall()
        seeded = 0
        skipped = 0
        for row in companies:
            domain = extract_domain(row["website"])
            if not domain:
                skipped += 1
                continue
            cur.execute(
                """
                INSERT INTO company_alias (company_id, alias, alias_type)
                VALUES (%s, %s, 'domain')
                ON CONFLICT (company_id, alias, alias_type) DO NOTHING
                """,
                (row["id"], domain),
            )
            status = "inserted" if cur.rowcount else "exists"
            print(f"  {row['name']:20s} -> {domain:40s} [{status}]")
            if cur.rowcount:
                seeded += 1
        print(f"[seed] domain aliases: {seeded} new, {skipped} companies had no usable website")

        # --- Step 3: verify ---
        cur.execute(
            """
            SELECT COUNT(*) AS n
              FROM company_alias
             WHERE alias_type = 'domain'
            """
        )
        n = cur.fetchone()["n"]
        print(f"[verify] total domain aliases in DB: {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
