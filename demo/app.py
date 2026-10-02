"""Deck Rank public demo.

Upload a pitch deck, see the values extracted with their source quote, and
see where the deck ranks against three fictional sample decks. No database:
everything lives in this browser session.

Run locally:  venv/bin/streamlit run demo/app.py
"""
import os
import sys
from pathlib import Path

# Streamlit puts demo/ on sys.path; the pipeline lives one level up.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

# Secrets from Streamlit Cloud become env vars, so llm_client finds the key.
try:
    for k in ("GEMINI_API_KEY", "DEMO_DAILY_LLM_CALLS"):
        if k in st.secrets:
            os.environ[k] = str(st.secrets[k])
except FileNotFoundError:
    pass  # no secrets file: local run, rules only unless the env var is set

from demo.engine import METRICS, analyse, draft_ask, rank  # noqa: E402
from demo.guard import SESSION_LLM_DECKS, BudgetedProvider, DailyBudget, check_upload  # noqa: E402

SAMPLES = ROOT / "demo" / "samples"
CASE_STUDY = "https://alexandrecela10.github.io/projects/deck-rank/"
REPO = "https://github.com/alexandrecela10/vc-company-scoring"

st.set_page_config(page_title="Deck Rank demo", page_icon="📄", layout="wide")


@st.cache_data(show_spinner=False)
def sample_results():
    """Sample decks run once per server start, rules only, so they cost nothing."""
    names = {"fintech_a_series_a": "Fintech A", "healthtech_b_seed": "Healthtech B", "climate_c_pre_seed": "Climate C"}
    return [analyse(names.get(p.stem, p.stem), p.read_bytes()) for p in sorted(SAMPLES.glob("*.pdf"))]


def company_name(filename: str) -> str:
    return Path(filename).stem.replace("_", " ").replace("-", " ")[:40]


budget = DailyBudget()
llm_available = bool(os.environ.get("GEMINI_API_KEY"))
st.session_state.setdefault("uploads", [])
st.session_state.setdefault("llm_decks", 0)

# --- Top: problem, player, how to try -------------------------------------
st.title("Deck Rank")
st.caption("Inbound pitch decks in, ranked queue out. Every value cites its slide.")
st.markdown(
    "**Problem.** The best inbound deals wait too long for a first call, and faster funds reach "
    "the founder first.  \n**For.** Investment analysts at early-stage VC funds.  \n"
    "**Try it.** 1. Upload a deck (PDF). 2. See where it ranks. 3. Open it to check each value against its slide."
)
st.info(
    "Use a sample or a public deck. Files stay in memory for this session and are not stored. "
    "With AI on, deck text is sent to Google Gemini's free tier, which may use it to improve its models."
)

# --- Upload ----------------------------------------------------------------
col_up, col_opts = st.columns([3, 2])
with col_up:
    up = st.file_uploader("Upload a pitch deck", type=["pdf"])
with col_opts:
    left = SESSION_LLM_DECKS - st.session_state.llm_decks
    can_llm = llm_available and left > 0 and budget.remaining() > 0
    use_llm = st.toggle("Use AI for metrics the rules miss", value=False, disabled=not can_llm)
    if not llm_available:
        st.caption("AI is off in this deployment. Rules only.")
    else:
        st.caption(f"AI decks left this session: {max(left, 0)}. Shared AI calls left today: {budget.remaining()}.")

if up is not None and st.button("Analyse deck", type="primary"):
    raw = up.getvalue()
    problem = check_upload(raw)
    if problem:
        st.error(problem)
    else:
        provider = None
        if use_llm and can_llm:
            from pipeline.extractors.llm_provider import GeminiProvider
            provider = BudgetedProvider(GeminiProvider(), budget)
        with st.spinner("Reading slides..."):
            result = analyse(company_name(up.name), raw, provider=provider)
        if provider is not None:
            st.session_state.llm_decks += 1
            result.used_llm = provider.calls > 0
        if result.error:
            st.error(f"Could not read this deck: {result.error}. Image-only PDFs have no text to extract.")
        else:
            st.session_state.uploads.append(result)

# --- Ranked queue ----------------------------------------------------------
decks = rank(sample_results() + st.session_state.uploads)
yours = {id(d) for d in st.session_state.uploads}

st.subheader("Ranked queue")
st.caption(
    "Ranked on the Financials score, the one must-have category a deck can support. "
    "The overall score also needs Technology Moat, Market Growth, Competitive Landscape and Unit Economics, "
    "which an analyst or an agent fills after the deck."
)
st.dataframe(pd.DataFrame([{
    "Rank": i + 1,
    "Company": d.name + ("  (yours)" if id(d) in yours else "  (sample)"),
    "Financials score (1-5)": d.rank_score,
    "Values found": len(d.values),
    "Deck metrics missing": len(d.missing_metrics),
    "AI used": "yes" if d.used_llm else "no",
} for i, d in enumerate(decks)]), hide_index=True, width="stretch")

# --- One deck in detail ------------------------------------------------------
st.subheader("Check a deck")
names = [d.name for d in decks]
pick = st.selectbox("Deck", names, index=len(names) - 1 if st.session_state.uploads else 0)
d = decks[names.index(pick)]

tab_score, tab_values, tab_gaps = st.tabs(["Why this rank", "Every value and its quote", "Gaps"])

with tab_score:
    if d.financials and d.financials.grounded_formula_inputs:
        st.markdown(f"**Financials: {d.rank_score} / 5.** Weighted average of the inputs the deck supports.")
        st.code(d.financials.grounded_formula, language=None)
        st.dataframe(pd.DataFrame([{
            "Component": i["component"].replace("_", " "),
            "Raw value": i["raw_value"],
            "Score (1-5)": i["derived_score"],
            "Weight": i["weight"],
            "Source": i["source_name"],
            "Quote": i["evidence"],
        } for i in d.financials.grounded_formula_inputs]), hide_index=True, width="stretch")
    else:
        st.warning("No Financials inputs found in this deck, so it ranks last.")

with tab_values:
    st.caption("Rules run first. AI values are kept only if their quote appears word for word on the slide.")
    st.dataframe(pd.DataFrame([{
        "Metric": METRICS[o.metric_name][0],
        "Value": o.value,
        "As of": o.as_of_date or "",
        "Slide": o.chunk_locator.replace("slide_", ""),
        "Quote": o.evidence_text,
        "Method": "AI" if o.method == "llm" else "rules",
    } for o in d.values]), hide_index=True, width="stretch")

with tab_gaps:
    if d.missing_metrics:
        st.markdown("**Not found in the deck:** " + ", ".join(METRICS[m][0] for m in d.missing_metrics if m in METRICS))
        st.text_area("Drafted request to the founder (not sent)", draft_ask(d.name, d.missing_metrics), height=220)
    else:
        st.success("Every deck metric was found.")
    st.markdown("**Must-have categories with no score yet:** " + ", ".join(d.missing_must_have_types))

st.divider()
st.caption(f"[Case study]({CASE_STUDY}) · [Code]({REPO}) · Sample decks are fictional.")
