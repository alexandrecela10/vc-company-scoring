"""
pipeline/ — the data producer side of the Company Scorer.

Everything under this package WRITES to the database: extractors,
ingestion, entity resolution, value resolution, scheduled jobs.

The Streamlit UI (app.py) is a READ-ONLY consumer of what this
package produces. See ARCHITECTURE.md §13 for the boundary rules.

Named `pipeline/` (not `platform/`) because `platform` is a Python
stdlib module — shadowing it would break transitive imports in
streamlit and google auth libs.
"""
