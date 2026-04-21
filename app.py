"""
Company Scorer — Streamlit UI

Layout:
  Sidebar      — company list with score badges, weight editor toggle
  Main panel   — tabs: Scorecard | Pipeline | Table Browser

Scorecard tab:
  - Overall score gauge + completeness status
  - Per metric type: score bar + child metrics with evidence
  - Missing must-haves panel + gap action status

Pipeline tab:
  - Stage history for selected company

Table Browser tab:
  - Raw view of any of the 8 DB tables (for demo transparency)
"""

import logging
import streamlit as st
import pandas as pd
import math
import time
from datetime import datetime
from typing import Optional

import db
import scorer
import market_agent
from scorer import CompanyScorecard, MetricTypeScore, MetricScore

# --- Logging config ---------------------------------------------------------
# Streamlit re-runs app.py from the top on EVERY user interaction (tab click,
# button press, etc.). Without the sentinel guard below, we'd re-init logging
# handlers on every rerun -- flooding pipeline.log with duplicate "session
# started" messages and opening N file descriptors. Configure once per process.
if not getattr(logging, "_scorer_configured", False):
    _log_format = logging.Formatter(
        "%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    )
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    # Wipe any handlers Streamlit installed so our format wins.
    _root.handlers.clear()

    _stream = logging.StreamHandler()
    _stream.setFormatter(_log_format)
    _root.addHandler(_stream)

    _file = logging.FileHandler("pipeline.log", mode="a", encoding="utf-8")
    _file.setFormatter(_log_format)
    _root.addHandler(_file)

    # Silence chatty libs -- we only care about OUR pipeline logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

    # Turn DEBUG on just for our pipeline so reject reasons (LLM null value,
    # canonicalisation fail, out-of-range, no retrieval hit) surface without
    # flooding the rest of the log with framework noise.
    logging.getLogger("pipeline").setLevel(logging.DEBUG)

    logging.getLogger("app").info("=" * 60)
    logging.getLogger("app").info("Streamlit process started -- logging to pipeline.log")
    # Sentinel: module-level attribute on logging so it survives Streamlit's
    # script reruns (each rerun re-imports our modules but logging keeps state).
    logging.Logger._scorer_configured = True  # type: ignore[attr-defined]
    logging._scorer_configured = True  # type: ignore[attr-defined]

# Track render start so we can show a live perf number in the sidebar footer.
# This is the single source of truth for "how long did this rerun take?"
_RENDER_START = time.perf_counter()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Company Scorer — Jasoor Ventures",
    page_icon="🏆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
if "selected_company_id" not in st.session_state:
    st.session_state.selected_company_id = None
# app_mode: 'browse' (per-company scorecard view) or 'upload' (standalone
# pitch-deck ingestion workflow). The Upload mode is intentionally NOT tied
# to a pre-selected company -- the entity resolver figures out the company
# from the deck itself. Starting in 'upload' on a cold load gives the user
# a clear entry point.
if "app_mode" not in st.session_state:
    st.session_state.app_mode = "browse"
if "user_id" not in st.session_state:
    st.session_state.user_id = "house"
if "show_weight_editor" not in st.session_state:
    st.session_state.show_weight_editor = False
# Two-tier cache invalidation:
#   - data_version: bumped for GLOBAL writes (weights, new companies).
#     Invalidates cross-company caches like the company list.
#   - company_versions[cid]: bumped for writes scoped to ONE company.
#     Lets us invalidate ONLY that company's scorecard/intel, so clicking
#     an override on company A doesn't force re-scoring of B, C, D.
if "data_version" not in st.session_state:
    st.session_state.data_version = 0
if "company_versions" not in st.session_state:
    st.session_state.company_versions = {}


def company_version(company_id: str) -> int:
    """Return the current cache-key version for this company.
    Combined with global data_version so either can trigger invalidation."""
    return st.session_state.company_versions.get(company_id, 0) + st.session_state.data_version


def bump_company(company_id: str):
    """Call after a write that affects ONLY this company.
    Examples: override, agent run on this company, stage transition, news fetch."""
    v = st.session_state.company_versions.get(company_id, 0)
    st.session_state.company_versions[company_id] = v + 1


def bump_data_version():
    """Call after a GLOBAL write (weights, schema, new company).
    Invalidates every cache that takes data_version as a key."""
    st.session_state.data_version += 1


# ---------------------------------------------------------------------------
# Cached data loaders
#
# Streamlit reruns the whole script on every click, so the sidebar would
# otherwise re-score every company on every interaction (~4s per click).
# These wrappers cache by (company_id, user_id, version). `version` is bumped
# by agent actions, overrides, and stage transitions so stale data never
# lingers after a real write.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=120, show_spinner=False)
def cached_score_company(company_id: str, user_id: str, version: int):
    """Score ONE company. Keyed by company version so a write to another
    company does NOT invalidate this entry — this is the main perf win."""
    return scorer.score_company(company_id, user_id)


@st.cache_data(ttl=120, show_spinner=False)
def cached_list_companies(version: int):
    """List all companies (id + name + industry) — cheap query, used by sidebar.
    Keyed by GLOBAL version only (the list itself rarely changes)."""
    return db.get_all_companies()


@st.cache_data(ttl=120, show_spinner=False)
def cached_get_company(company_id: str, version: int):
    return db.get_company(company_id)


@st.cache_data(ttl=120, show_spinner=False)
def cached_get_founders(company_id: str, version: int):
    return db.get_founders(company_id)


@st.cache_data(ttl=120, show_spinner=False)
def cached_get_news(company_id: str, version: int):
    return db.get_cached_news(company_id, limit=5)


@st.cache_data(ttl=120, show_spinner=False)
def cached_newest_news_fetch(company_id: str, version: int):
    return db.newest_news_fetch(company_id)


@st.cache_data(ttl=120, show_spinner=False)
def cached_pipeline_history(company_id: str, version: int):
    return db.get_pipeline_history(company_id)


@st.cache_data(ttl=300, show_spinner=False)
def cached_get_metric_types(version: int):
    # Metric types change rarely — longer TTL is fine.
    return db.get_metric_types()


@st.cache_data(ttl=120, show_spinner=False)
def cached_stale_company_ids(version: int) -> set:
    """
    Return the set of company_ids that have at least one stale Market Growth
    value. Used in the sidebar to show a ⏳ badge. Cheap single query,
    cached per data_version.
    """
    rows = market_agent.find_stale_market_values()
    return {r["company_id"] for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def score_color(score: Optional[float]) -> str:
    """Return a colour string based on score value for visual badges."""
    if score is None:
        return "#888888"
    if score >= 4.0:
        return "#22c55e"   # green
    if score >= 3.0:
        return "#f59e0b"   # amber
    return "#ef4444"       # red


def score_badge(score: Optional[float], size: str = "1rem") -> str:
    """HTML badge showing the score with colour coding."""
    color = score_color(score)
    label = f"{score:.2f}" if score is not None else "N/A"
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:12px;font-weight:700;font-size:{size}">{label}</span>'
    )


def progress_bar_html(score: Optional[float], max_score: float = 5.0) -> str:
    """Thin horizontal progress bar for a score."""
    if score is None:
        pct = 0
        color = "#888888"
    else:
        pct = min(100, (score / max_score) * 100)
        color = score_color(score)
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:{color};width:{pct:.1f}%;height:8px;border-radius:4px"></div>'
        f"</div>"
    )


def pipeline_stage_label(stage: str) -> str:
    """Human-readable pipeline stage label."""
    labels = {
        "deal_sourcing": "🔍 Deal Sourcing",
        "first_contact": "📞 First Contact",
        "due_diligence": "🔬 Due Diligence",
        "ic_review": "⚖️ IC Review",
        "passed": "❌ Passed",
        "invested": "✅ Invested",
    }
    return labels.get(stage, stage)


# ---------------------------------------------------------------------------
# Sidebar — company list
# ---------------------------------------------------------------------------

def render_sidebar():
    """Render the company list sidebar with score badges."""
    with st.sidebar:
        st.markdown("## 🏆 Company Scorer")
        st.markdown("*Jasoor Ventures — Deal Sourcing*")
        st.divider()

        # --- Global workflows (not tied to any company) --------------------
        # Upload is its own surface because a new deck may belong to a NEW
        # company we don't have yet -- the entity resolver decides, not the UI.
        upload_label = "📤 Upload a pitch deck"
        if st.session_state.app_mode == "upload":
            upload_label = "◀ Back to companies"
        if st.button(upload_label, use_container_width=True, key="sidebar.upload_toggle"):
            st.session_state.app_mode = (
                "browse" if st.session_state.app_mode == "upload" else "upload"
            )
            st.rerun()

        st.divider()

        # Weight view toggle
        view = st.radio(
            "Weight view",
            ["House view", "Personal view"],
            horizontal=True,
            help="House view uses institutional weights. Personal view uses your custom weights.",
        )
        st.session_state.user_id = "house" if view == "House view" else st.session_state.get("analyst_email", "house")

        if st.button("⚖️ Edit weights", use_container_width=True):
            st.session_state.show_weight_editor = not st.session_state.show_weight_editor

        st.divider()
        st.markdown("### Companies")

        # Per-company cached scoring: each company's score is cached under its
        # own version key, so writes to company A do NOT invalidate B/C/D.
        # On a rerun where nothing changed, this loop is all cache hits (~instant).
        try:
            companies = cached_list_companies(st.session_state.data_version)
            all_scorecards = []
            for c in companies:
                try:
                    sc = cached_score_company(
                        c["id"],
                        st.session_state.user_id,
                        company_version(c["id"]),
                    )
                    all_scorecards.append(sc)
                except Exception as e:
                    # Per-company failure shouldn't kill the whole sidebar
                    st.warning(f"Scoring failed for {c.get('name','?')}: {e}")
            # Sort: scored first (high→low), then incomplete at the bottom
            all_scorecards.sort(
                key=lambda s: (s.overall_score is None, -(s.overall_score or 0))
            )
        except Exception as e:
            st.error(f"Could not load companies: {e}")
            return

        if not all_scorecards:
            st.info("No companies found. Run seed.sql in Supabase.")
            return

        # Cached — one query per data_version, not per company
        stale_ids = cached_stale_company_ids(st.session_state.data_version)

        for sc in all_scorecards:
            badge_html = score_badge(sc.overall_score, size="0.8rem")
            stage_icon = pipeline_stage_label(sc.pipeline_stage).split(" ")[0]
            is_selected = st.session_state.selected_company_id == sc.company_id

            # Highlight selected company
            bg = "#1e3a5f" if is_selected else "transparent"
            border = "2px solid #3b82f6" if is_selected else "1px solid #374151"

            clicked = st.button(
                f"{stage_icon} {sc.company_name}",
                key=f"company_{sc.company_id}",
                use_container_width=True,
                help=f"Score: {sc.overall_score or 'Incomplete'}",
            )
            if clicked:
                # Clicking a company implies we want the per-company view --
                # even if we were in upload mode. This keeps the sidebar as
                # the single source of navigation.
                st.session_state.selected_company_id = sc.company_id
                st.session_state.app_mode = "browse"
                st.rerun()

            # Show score badge + issue indicators below button
            col_score, col_issues = st.columns([2, 1])
            with col_score:
                st.markdown(badge_html, unsafe_allow_html=True)
            with col_issues:
                indicators = []
                if not sc.is_complete:
                    indicators.append(
                        '<span style="color:#f59e0b;font-size:0.7rem">⚠️ Incomplete</span>'
                    )
                if sc.company_id in stale_ids:
                    indicators.append(
                        '<span style="color:#60a5fa;font-size:0.7rem" '
                        'title="Has stale Market Growth signal — run market agent">'
                        '⏳ Stale</span>'
                    )
                if indicators:
                    st.markdown("<br>".join(indicators), unsafe_allow_html=True)

            st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

        # Auto-select first company
        if st.session_state.selected_company_id is None and all_scorecards:
            st.session_state.selected_company_id = all_scorecards[0].company_id
            st.rerun()


# ---------------------------------------------------------------------------
# Weight editor panel
# ---------------------------------------------------------------------------

def render_weight_editor():
    """Inline weight editor — sliders per metric type."""
    st.markdown("### ⚖️ Metric Type Weights")
    st.caption(
        "Adjust weights to reflect your personal view. "
        "The score recomputes instantly. House = equal weights."
    )

    metric_types = db.get_metric_types()
    if not metric_types:
        st.info("No metric types found.")
        return

    cols = st.columns(2)
    for i, mt in enumerate(metric_types):
        with cols[i % 2]:
            current_weights = db.get_weights(st.session_state.user_id)
            current_w = current_weights.get(mt["id"], mt.get("house_weight", 1.0))
            new_w = st.slider(
                mt["name"],
                min_value=0.0,
                max_value=3.0,
                value=float(current_w),
                step=0.1,
                key=f"weight_{mt['id']}",
                help="0 = ignore this dimension entirely, 3 = triple weight",
            )
            if new_w != current_w:
                db.upsert_weight(st.session_state.user_id, mt["id"], new_w)
                # Weights are GLOBAL — affect every company's score.
                bump_data_version()

    st.divider()


# ---------------------------------------------------------------------------
# Scorecard tab
# ---------------------------------------------------------------------------

def render_overall_score(sc: CompanyScorecard):
    """Top-of-scorecard overall score + formula breakdown."""
    col_score, col_meta, col_formula = st.columns([1, 2, 2])

    with col_score:
        # Big score display
        color = score_color(sc.overall_score)
        label = f"{sc.overall_score:.2f}" if sc.overall_score else "N/A"
        st.markdown(
            f"""
            <div style="text-align:center;padding:16px;background:#1f2937;
                        border-radius:12px;border:2px solid {color}">
                <div style="font-size:2.8rem;font-weight:800;color:{color}">{label}</div>
                <div style="color:#9ca3af;font-size:0.8rem">out of 5.00</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_meta:
        st.markdown(f"**Company:** {sc.company_name}")
        st.markdown(f"**Pipeline stage:** {pipeline_stage_label(sc.pipeline_stage)}")
        if sc.is_complete:
            st.markdown("**Status:** ✅ All must-haves present")
        else:
            st.markdown("**Status:** ⚠️ Score incomplete")
            for mt_name in sc.missing_must_have_types:
                st.markdown(f"&nbsp;&nbsp;&nbsp;• Missing: **{mt_name}**", unsafe_allow_html=True)

    with col_formula:
        st.markdown("**Formula breakdown (Option B)**")
        if sc.weighted_avg_base is not None:
            st.markdown(f"Base weighted avg: `{sc.weighted_avg_base:.2f}`")
            st.markdown(f"Must-have multiplier: `{sc.must_have_multiplier:.4f}`")
            if sc.overall_score:
                st.markdown(
                    f"**Overall = {sc.weighted_avg_base:.2f} × {sc.must_have_multiplier:.4f} = {sc.overall_score:.2f}**"
                )
        else:
            st.markdown("*Not computable — missing must-have data*")


def render_provenance_expander(company_id: str, mts: MetricTypeScore):
    """
    Phase 1 provenance panel: for each metric in this dimension, show the
    full chain: extractor → extraction run → observation → source document.

    Rationale (ARCHITECTURE.md §6):
      Every metric value must be auditable down to the exact observation
      that produced it. Analysts should be able to answer "where did this
      number come from?" in one click — no LLM call, no spelunking in the
      DB. We query db.get_provenance_chain once per metric and render a
      compact table.

    Kept inside an st.expander so it doesn't clutter the default view —
    shown only when the analyst asks for it.
    """
    with st.expander("🔗 Provenance & sources", expanded=False):
        rows = []
        for ms in mts.metric_scores:
            chain = db.get_provenance_chain(company_id, ms.metric_id)
            if not chain:
                continue

            # Two display modes depending on whether this is an analyst
            # override (no observation) or a derived observation.
            if chain["override"]:
                extractor_display = "🔒 analyst_override"
                source_display    = "analyst note"
                run_time          = chain["cmv_captured_at"]
            else:
                extractor_display = (
                    f"{chain['extractor_name']} v{chain['extractor_version']}"
                    if chain["extractor_name"] else "—"
                )
                # Prefer a real source doc when we have one; fall back to the
                # evidence URL (Phase 2+ will populate source_document_id).
                if chain["source_origin_path"]:
                    source_display = f"📄 {chain['source_origin_path']}"
                elif chain["observation_evidence_url"]:
                    source_display = chain["observation_evidence_url"]
                else:
                    source_display = "—"
                run_time = chain["run_started_at"] or chain["observation_captured_at"]

            rows.append({
                "Metric":      ms.metric_name,
                "Value":       ms.value_raw or "—",
                "Extractor":   extractor_display,
                "When":        run_time.strftime("%Y-%m-%d %H:%M") if run_time else "—",
                "Confidence":  f"{ms.confidence:.0%}",
                "Source":      source_display,
                "Evidence":    (chain["observation_evidence_text"]
                                or chain["override_reason"]
                                or "—")[:200],
            })

        if not rows:
            st.caption("No provenance data yet — run the Phase 1 backfill.")
            return

        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Source":   st.column_config.TextColumn(width="medium"),
                "Evidence": st.column_config.TextColumn(width="large"),
            },
        )


def render_metric_type_section(company_id: str, mts: MetricTypeScore):
    """Render one metric type block: score bar + child metrics table."""
    # Header row
    badge_html = score_badge(mts.type_score)
    must_have_tag = (
        '<span style="background:#7c3aed;color:white;padding:1px 6px;'
        'border-radius:8px;font-size:0.7rem;margin-left:8px">must-have</span>'
        if mts.must_have else ""
    )

    st.markdown(
        f"**{mts.metric_type_name}** {must_have_tag} &nbsp; {badge_html}",
        unsafe_allow_html=True,
    )
    st.markdown(progress_bar_html(mts.type_score), unsafe_allow_html=True)

    if mts.missing_must_haves:
        for name in mts.missing_must_haves:
            st.markdown(
                f'<span style="color:#f59e0b;font-size:0.8rem">⚠️ Must-have metric missing: {name}</span>',
                unsafe_allow_html=True,
            )

    if not mts.metric_scores:
        st.caption("No metrics recorded yet for this dimension.")
        return

    # Build metrics table.
    # "Link" and "Verified" columns expose the evidence URL + the deterministic
    # check from link_verifier. ✅ = URL reachable AND quote appears on the page.
    # ⚠️ = URL present but not verified (may be hallucinated or paywalled).
    # — = no URL at all (analyst-entered or agent had no source).
    rows = []
    for ms in mts.metric_scores:
        if ms.evidence_url:
            verified_badge = "✅" if ms.url_verified else "⚠️"
        else:
            verified_badge = "—"
        rows.append({
            "Metric": ms.metric_name,
            "Value": ms.value_raw or "—",
            "Score (1–5)": f"{ms.value_numeric:.1f}" if ms.value_numeric is not None else "—",
            "Weight": ms.weight,
            "Source": ms.source_name or "—",
            "Confidence": f"{ms.confidence:.0%}",
            "🔒": "✅" if ms.override else "",
            "Evidence": (ms.raw_evidence[:120] + "…") if ms.raw_evidence and len(ms.raw_evidence) > 120 else (ms.raw_evidence or "—"),
            "Link": ms.evidence_url or "",
            "Verified": verified_badge,
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Evidence": st.column_config.TextColumn(width="large"),
            "Score (1–5)": st.column_config.TextColumn(width="small"),
            "Confidence": st.column_config.TextColumn(width="small"),
            "🔒": st.column_config.TextColumn(width="small", help="Analyst override — agents cannot modify"),
            "Link": st.column_config.LinkColumn(width="small", display_text="open ↗"),
            "Verified": st.column_config.TextColumn(
                width="small",
                help="✅ URL reachable and evidence quote found on page  |  ⚠️ link present but not verified  |  — no source link",
            ),
        },
    )

    # Phase 1 provenance: one expander per dimension. Hidden by default so
    # the scorecard stays tight; opens to reveal extractor + run + source.
    render_provenance_expander(company_id, mts)


def render_market_agent(company_id: str):
    """
    Show staleness of this company's Market Growth metrics and
    let the analyst trigger a refresh via the market_agent.

    The agent will: Tavily search → Gemini extract → insert new metric value
    with provenance. Only non-override rows are refreshed.
    """
    # Only show stale rows for THIS company (not all companies)
    all_stale = market_agent.find_stale_market_values()
    stale_for_this = [s for s in all_stale if s["company_id"] == company_id]

    st.markdown("### 📈 Market Signal Agent")

    if not stale_for_this:
        st.success(
            "All Market Growth metrics are fresh (updated within the last "
            f"{market_agent.STALENESS_DAYS} days)."
        )
        return

    st.warning(
        f"Found **{len(stale_for_this)} stale** Market Growth metric(s) "
        f"older than {market_agent.STALENESS_DAYS} days. "
        "The agent will use Tavily + Gemini to refresh them with a fresh value + evidence."
    )

    # Show each stale row with its age
    for s in stale_for_this:
        age_days = (datetime.now(s["captured_at"].tzinfo) - s["captured_at"]).days
        st.markdown(
            f"- **{s['metric_name']}** = `{s['current_value']}` "
            f"(last updated {age_days} days ago)"
        )

    if st.button("🔄 Refresh stale signals now", type="primary", key=f"mkt_{company_id}"):
        with st.spinner("Searching the web and extracting latest market data..."):
            results = [market_agent.refresh_one(s) for s in stale_for_this]
        bump_company(company_id)  # only this company's data changed

        # Show each result inline
        for r in results:
            if r["status"] == "updated":
                verified = r.get("url_verified", False)
                verify_badge = "✅ verified" if verified else f"⚠️ unverified ({r.get('url_verify_reason','')})"
                st.success(
                    f"✅ {r['message']} → new value: **{r['new_value']}**  ·  link {verify_badge}"
                )
                with st.expander("View evidence"):
                    st.markdown(f"**Source:** [{r['source_url']}]({r['source_url']})")
                    st.markdown(f'> "{r["evidence_quote"]}"')
            elif r["status"] == "no_data":
                st.info(f"ℹ️ {r['message']}")
            else:
                st.error(f"❌ {r['message']}")
        st.rerun()


def render_gap_actions(company_id: str):
    """
    Gap Agent panel.

    Shows:
      - missing must-have metrics (with the obtain_method routing hint)
      - a "Run gap agent" button that fires gap_agent.process_company
        (drafts founder emails for "Ask Founders" metrics, runs Tavily
        + Gemini + link verification for web-findable metrics)
      - the log of previously-attempted actions with their status
    """
    import gap_agent

    st.markdown("### 🤖 Gap Agent")

    missing = scorer.get_missing_must_haves(company_id)

    if missing:
        st.warning(
            f"**{len(missing)} missing must-have metric(s).** "
            "The agent can draft founder emails or run web searches to close these gaps."
        )
        for m in missing:
            route = "📧 Founder outreach" if m["obtain_method"] == "Ask Founders" else \
                    "🔍 Web search" if m["obtain_method"] in {"Tavily", "LinkedIn", "Web Search"} else \
                    "✋ Manual"
            st.markdown(
                f"- **{m['metric_name']}** ({m['metric_type_name']}) — "
                f"obtain method: `{m['obtain_method']}` → {route}"
            )

        if st.button("🤖 Run gap agent now", type="primary", key=f"gap_{company_id}"):
            with st.spinner("Drafting emails and searching the web..."):
                results = gap_agent.process_company(company_id)
            bump_company(company_id)  # only this company's gaps changed
            for r in results:
                icon = {"filled": "✅", "drafted": "✉️", "no_data": "ℹ️",
                        "error": "❌", "skipped": "⚠️"}.get(r["status"], "•")
                st.write(f"{icon} {r['message']}")
            st.rerun()
    else:
        st.success("No missing must-have metrics. 🎉")

    # Always show the action log (even if no gaps currently missing)
    actions = db.get_gap_actions(company_id)
    if not actions:
        return

    st.markdown("#### Action log")
    for action in actions:
        metric_name = (action.get("metric") or {}).get("name", "Unknown metric")
        status_color = {"pending": "#f59e0b", "sent": "#3b82f6", "resolved": "#22c55e", "failed": "#ef4444"}.get(
            action.get("status", "pending"), "#888888"
        )
        status_badge = (
            f'<span style="background:{status_color};color:white;padding:2px 8px;'
            f'border-radius:8px;font-size:0.75rem">{action.get("status","pending").upper()}</span>'
        )
        gap_type_label = "📧 Founder Outreach" if action.get("gap_type") == "outreach" else "🔍 Web Search"

        with st.expander(f"{gap_type_label} — {metric_name}", expanded=False):
            st.markdown(f"**Type:** {gap_type_label} &nbsp; {status_badge}", unsafe_allow_html=True)
            st.markdown(f"**Created:** {str(action.get('created_at',''))[:10]}")
            if action.get("output_draft"):
                st.markdown("**Draft output:**")
                st.markdown(
                    f'<div style="background:#1f2937;padding:12px;border-radius:8px;'
                    f'font-size:0.85rem;white-space:pre-wrap">{action["output_draft"]}</div>',
                    unsafe_allow_html=True,
                )

            # Status update buttons
            col_a, col_b = st.columns(2)
            with col_a:
                if action.get("status") == "pending":
                    if st.button("Mark as Sent", key=f"sent_{action['id']}"):
                        db.update_gap_action_status(action["id"], "sent")
                        bump_company(company_id)
                        st.rerun()
            with col_b:
                if action.get("status") in ("pending", "sent"):
                    if st.button("Mark Resolved", key=f"resolved_{action['id']}"):
                        db.update_gap_action_status(action["id"], "resolved")
                        bump_company(company_id)
                        st.rerun()


def render_override_editor(company_id: str, sc: CompanyScorecard):
    """Allow analyst to override a metric value directly in the UI."""
    st.markdown("### ✏️ Override a Metric Value")
    st.caption("Overridden values are locked — agents cannot overwrite them.")

    # Build flat list of all metrics with current values
    all_options = {}
    for mts in sc.type_scores:
        for ms in mts.metric_scores:
            label = f"{mts.metric_type_name} → {ms.metric_name}"
            all_options[label] = ms

    selected_label = st.selectbox("Select metric to override", list(all_options.keys()))
    if not selected_label:
        return

    selected_ms = all_options[selected_label]

    col_val, col_reason = st.columns(2)
    with col_val:
        new_value = st.text_input(
            "New value",
            value=selected_ms.value_raw or "",
            help="Use 'true'/'false' for booleans, 1–5 for scores, numbers for counts",
        )
    with col_reason:
        override_reason = st.text_input(
            "Reason for override",
            placeholder="e.g. Confirmed by founder in call 2025-04-18",
        )

    if st.button("💾 Save Override", type="primary"):
        if new_value and override_reason:
            db.upsert_metric_value(
                company_id=company_id,
                metric_id=selected_ms.metric_id,
                value=new_value,
                source_id=None,
                raw_evidence=f"[ANALYST OVERRIDE] {override_reason}",
                captured_by=st.session_state.user_id,
                confidence=1.0,
                override=True,
                override_reason=override_reason,
            )
            bump_company(company_id)  # this company's score will recompute
            st.success(f"Override saved for **{selected_ms.metric_name}**. Score will recompute.")
            st.rerun()
        else:
            st.warning("Please provide both a new value and a reason.")


def render_company_intel(company_id: str, sc):
    """
    Rich company card: website, LinkedIn, founders, recent news, and
    quick-action buttons (stage transitions + Share).

    Sits between the score header and the dimension breakdown so analysts
    can scan "who/where/what's new" before diving into metrics.
    """
    import company_intel

    company = cached_get_company(company_id, company_version(company_id))
    if not company:
        return

    # ---------- Discovery provenance banner ----------
    # If this company was found by Alpha Scout, surface the trust signals:
    # grounding score + the source URL the evidence came from. This is the
    # single most important UX moment for demo — we SHOW the audit trail.
    if company.get("discovery_source_url") or company.get("source_channel") == "alpha_scout":
        grounding = company.get("discovery_grounding_score")
        grounding_pct = f"{grounding:.0%}" if grounding is not None else "n/a"
        # Colour the pill: green ≥80%, amber 50–79%, red <50%
        pill_color = (
            "#065f46" if (grounding or 0) >= 0.8
            else "#92400e" if (grounding or 0) >= 0.5
            else "#7f1d1d"
        )
        src_link = (
            f' · <a href="{company["discovery_source_url"]}" target="_blank" '
            f'style="color:#93c5fd">source</a>'
            if company.get("discovery_source_url") else ""
        )
        st.markdown(
            f'<div style="background:{pill_color};color:white;padding:4px 10px;'
            f'border-radius:6px;display:inline-block;font-size:0.8rem;margin-bottom:8px">'
            f'🔎 Discovered by Alpha Scout · grounding {grounding_pct}{src_link}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ---------- Links row ----------
    # ✓ badge on website if we HTTP-verified it during discovery.
    link_bits = []
    if company.get("website"):
        verified_badge = " ✓" if company.get("website_verified") else ""
        link_bits.append(f"🌐 [Website{verified_badge}]({company['website']})")
    if company.get("linkedin_url"):
        link_bits.append(f"💼 [Company LinkedIn]({company['linkedin_url']})")
    if link_bits:
        st.markdown(" &nbsp; · &nbsp; ".join(link_bits))

    # ---------- Founders ----------
    founders = cached_get_founders(company_id, company_version(company_id))
    if founders:
        st.markdown("#### Founders")
        cols = st.columns(min(len(founders), 3))
        for i, f in enumerate(founders):
            with cols[i % len(cols)]:
                # Compact card per founder. LinkedIn is the primary CTA,
                # but ONLY if verified — otherwise we show an ⚠️ warning so
                # the analyst knows not to trust the link blindly.
                linkedin_url = f.get("linkedin_url")
                if linkedin_url and f.get("linkedin_verified"):
                    # Green check — snippet confirmed this LinkedIn is for our company
                    linkedin_html = (
                        f"[💼 LinkedIn ✓]({linkedin_url})"
                    )
                elif linkedin_url:
                    # URL found but snippet didn't mention company → show big warning
                    linkedin_html = (
                        f'<a href="{linkedin_url}" target="_blank" '
                        f'style="color:#f59e0b">⚠️ LinkedIn (unverified)</a>'
                        f'<br><span style="font-size:0.7rem;color:#9ca3af">'
                        f'Search snippet did not mention the company — verify manually</span>'
                    )
                else:
                    linkedin_html = "_no LinkedIn_"

                st.markdown(
                    f"**{f['name']}**  \n"
                    f"<span style='color:#9ca3af;font-size:0.85rem'>{f.get('title','')}</span>  \n"
                    f"{linkedin_html}",
                    unsafe_allow_html=True,
                )

    # ---------- News ----------
    st.markdown("#### Recent news")
    news = cached_get_news(company_id, company_version(company_id))
    last_fetch_row = cached_newest_news_fetch(company_id, company_version(company_id)) or {}
    last_fetch = last_fetch_row.get("last_fetch")

    col_news, col_btn = st.columns([4, 1])
    with col_btn:
        if st.button("🔄 Refresh news", key=f"news_{company_id}"):
            with st.spinner("Searching the web..."):
                try:
                    n = company_intel.fetch_news(company_id)
                    st.success(f"Fetched {n} articles.")
                except Exception as e:
                    st.error(f"News fetch failed: {e}")
            bump_company(company_id)
            st.rerun()

    with col_news:
        if not news:
            st.caption("No news cached yet. Click 'Refresh news' to fetch.")
        else:
            if last_fetch:
                st.caption(f"Last refreshed: {str(last_fetch)[:16]} UTC")
            for a in news:
                st.markdown(f"- [{a['title']}]({a['url']})")
                if a.get("snippet"):
                    st.caption(a["snippet"][:180] + ("…" if len(a["snippet"]) > 180 else ""))

    # ---------- Action buttons ----------
    st.markdown("#### Actions")
    current_stage = company.get("pipeline_stage", "deal_sourcing")
    col_a, col_b, col_c = st.columns(3)

    # Move to First Contact (only if not already there or later)
    with col_a:
        already_past = current_stage not in ("deal_sourcing",)
        if st.button(
            "➡️ Move to First Contact",
            key=f"fc_{company_id}",
            disabled=already_past,
            help="Already past First Contact stage" if already_past else None,
        ):
            company_intel.transition_stage(
                company_id, "first_contact",
                user_id=st.session_state.user_id,
                score_snapshot=sc.overall_score,
            )
            bump_company(company_id)
            st.success("Moved to First Contact.")
            st.rerun()

    # Pass
    with col_b:
        is_passed = current_stage == "passed"
        if st.button(
            "🚫 Pass",
            key=f"pass_{company_id}",
            disabled=is_passed,
            help="Already passed" if is_passed else None,
        ):
            company_intel.transition_stage(
                company_id, "passed",
                user_id=st.session_state.user_id,
                score_snapshot=sc.overall_score,
            )
            bump_company(company_id)
            st.warning("Marked as passed.")
            st.rerun()

    # Share — generates a copyable markdown brief
    with col_c:
        # Use session state to toggle visibility so button is non-destructive
        share_key = f"show_share_{company_id}"
        if st.button("📤 Share with team", key=f"share_{company_id}"):
            st.session_state[share_key] = not st.session_state.get(share_key, False)

    if st.session_state.get(share_key):
        summary = company_intel.build_share_summary(company, sc, founders, news)
        st.info("Copy the markdown below and paste into Slack/email:")
        # st.code has a built-in copy button in the top-right corner
        st.code(summary, language="markdown")


def render_scorecard_tab(company_id: str):
    """Full scorecard tab for one company."""
    try:
        sc = cached_score_company(
            company_id,
            st.session_state.user_id,
            company_version(company_id),
        )
    except Exception as e:
        st.error(f"Could not compute scorecard: {e}")
        return

    # Overall score header
    render_overall_score(sc)

    # Company intel — website, LinkedIns, founders, news, action buttons
    render_company_intel(company_id, sc)

    st.divider()

    # Per metric type sections
    st.markdown("### Dimension Breakdown")

    # Must-haves first, then nice-to-haves
    must_haves = [mts for mts in sc.type_scores if mts.must_have]
    nice_to_haves = [mts for mts in sc.type_scores if not mts.must_have]

    for mts in must_haves:
        with st.container():
            render_metric_type_section(company_id, mts)
            st.markdown("<div style='margin-bottom:16px'></div>", unsafe_allow_html=True)

    if nice_to_haves:
        with st.expander("📊 Nice-to-have dimensions", expanded=False):
            for mts in nice_to_haves:
                render_metric_type_section(company_id, mts)
                st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

    st.divider()

    # Market agent panel
    render_market_agent(company_id)

    # Gap actions panel
    render_gap_actions(company_id)

    # Override editor
    with st.expander("✏️ Override a metric value (analyst lock)", expanded=False):
        render_override_editor(company_id, sc)


# ---------------------------------------------------------------------------
# Pipeline tab
# ---------------------------------------------------------------------------

def render_pipeline_tab(company_id: str):
    """Pipeline stage history for one company."""
    v = company_version(company_id)
    history = cached_pipeline_history(company_id, v)
    company = cached_get_company(company_id, v)

    st.markdown(f"### Pipeline — {company['name'] if company else ''}")
    st.markdown(
        f"**Current stage:** {pipeline_stage_label(company.get('pipeline_stage', '') if company else '')}"
    )

    # Stage transition buttons
    stages = ["deal_sourcing", "first_contact", "due_diligence", "ic_review", "passed", "invested"]
    current = company.get("pipeline_stage", "deal_sourcing") if company else "deal_sourcing"

    st.markdown("**Move to stage:**")
    cols = st.columns(len(stages))
    for i, stage in enumerate(stages):
        with cols[i]:
            is_current = stage == current
            if st.button(
                pipeline_stage_label(stage),
                key=f"stage_{stage}",
                disabled=is_current,
                type="primary" if is_current else "secondary",
            ):
                sc = scorer.score_company(company_id, st.session_state.user_id)
                db.add_pipeline_event(
                    company_id=company_id,
                    from_stage=current,
                    to_stage=stage,
                    changed_by=st.session_state.user_id,
                    score_snapshot=sc.overall_score,
                    triggered_by="manual",
                )
                # Update current stage on company row
                db.update_company_stage(company_id, stage)
                bump_company(company_id)  # invalidate this company's caches
                st.rerun()

    st.divider()
    st.markdown("### Stage History")

    if not history:
        st.info("No stage transitions recorded yet.")
        return

    rows = []
    for event in history:
        rows.append({
            "Date": str(event.get("changed_at", ""))[:10],
            "From": pipeline_stage_label(event.get("from_stage", "")),
            "To": pipeline_stage_label(event.get("to_stage", "")),
            "Changed by": event.get("changed_by", ""),
            "Triggered by": event.get("triggered_by", ""),
            "Score at transition": f"{event['score_snapshot']:.2f}" if event.get("score_snapshot") else "—",
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Table Browser tab
# ---------------------------------------------------------------------------

def render_table_browser():
    """Raw table viewer for all 8 DB tables — for demo transparency."""
    st.markdown("### 🗃️ Table Browser")
    st.caption(
        "Browse the raw database tables. This shows the exact data structure "
        "powering the scoring system — full audit trail."
    )

    tables = [
        "company",
        "metric_type",
        "metric",
        "data_source",
        "company_metric_value",
        "user_weight",
        "pipeline_event",
        "gap_action",
    ]

    selected_table = st.selectbox("Select table", tables)

    try:
        rows = db.get_table_rows(selected_table, limit=200)
    except Exception as e:
        st.error(f"Could not load table: {e}")
        return

    if not rows:
        st.info(f"Table `{selected_table}` is empty.")
        return

    df = pd.DataFrame(rows)
    st.caption(f"{len(df)} rows from `{selected_table}`")
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

def main():
    render_sidebar()

    if st.session_state.show_weight_editor:
        render_weight_editor()

    # --- Branch on app mode -------------------------------------------------
    # Upload mode is a standalone workflow: no company pre-selected, no tabs,
    # full width for the ingestion flow. Entity resolution happens inside.
    if st.session_state.app_mode == "upload":
        from ui_upload import render_upload_tab
        render_upload_tab()
        elapsed_ms = (time.perf_counter() - _RENDER_START) * 1000
        with st.sidebar:
            st.caption(f"⚡ render: {elapsed_ms:.0f} ms")
        return

    company_id = st.session_state.selected_company_id
    if not company_id:
        st.info("Select a company from the sidebar to view its scorecard.")
        return

    # Cached header read — was the single biggest uncached call (every rerun).
    company = cached_get_company(company_id, company_version(company_id))
    company_name = company["name"] if company else "Unknown"

    st.markdown(f"# {company_name}")
    st.markdown(
        f'<span style="color:#9ca3af;font-size:0.9rem">'
        f'{company.get("industry","") if company else ""} · '
        f'{company.get("country","") if company else ""} · '
        f'{company.get("source_type","").title() if company else ""} via '
        f'{company.get("source_channel","") if company else ""}'
        f"</span>",
        unsafe_allow_html=True,
    )
    st.divider()

    tab_scorecard, tab_pipeline, tab_tables = st.tabs(
        ["🏆 Scorecard", "📋 Pipeline", "️ Table Browser"]
    )

    with tab_scorecard:
        render_scorecard_tab(company_id)

    with tab_pipeline:
        render_pipeline_tab(company_id)

    with tab_tables:
        render_table_browser()

    # Live perf readout — how long this rerun took end-to-end.
    # Helps us see the impact of caching changes without external profiling.
    elapsed_ms = (time.perf_counter() - _RENDER_START) * 1000
    with st.sidebar:
        st.caption(f"⚡ render: {elapsed_ms:.0f} ms")


if __name__ == "__main__":
    main()
