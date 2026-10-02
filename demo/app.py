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
st.caption("Founders email pitch decks (slides asking for money) to a startup fund. This ranks them so the best one is read first.")
st.markdown(
    "**Problem.** A fund gets many decks a week and reads them in the order they arrive. The best one can wait "
    "days, and a faster fund meets the founder first.  \n**For.** Junior investors at startup funds.  \n"
    "**Try it.** 1. Upload a deck (PDF), or use the 3 made-up samples. 2. See where it ranks. "
    "3. Open it and check each number against the slide it came from."
)
st.info(
    "Use a sample or a public deck. Files stay in this browser session only and are never saved. "
    "If you turn AI on, the deck text goes to Google's free AI service, which may use it to improve its models."
)

# --- Upload ----------------------------------------------------------------
col_up, col_opts = st.columns([3, 2])
with col_up:
    up = st.file_uploader("Upload a pitch deck", type=["pdf"])
with col_opts:
    left = SESSION_LLM_DECKS - st.session_state.llm_decks
    can_llm = llm_available and left > 0 and budget.remaining() > 0
    use_llm = st.toggle("Use AI for numbers the fixed rules miss", value=False, disabled=not can_llm)
    if not llm_available:
        st.caption("AI is off here. Fixed rules only, free.")
    else:
        st.caption(f"AI decks you have left: {max(left, 0)}. AI requests left today for everyone: {budget.remaining()}.")

if up is not None and st.button("Read and rank this deck", type="primary"):
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
            st.error(f"Could not read this deck: {result.error}. Decks saved as images have no text to read.")
        else:
            st.session_state.uploads.append(result)

# --- Ranked queue ----------------------------------------------------------
decks = rank(sample_results() + st.session_state.uploads)
yours = {id(d) for d in st.session_state.uploads}

st.subheader("Ranked list: read from the top")
st.caption(
    "Ranked on the money score (margin, months of cash left, revenue against spending, funding round), "
    "the one must-have score a deck alone can support. A full score also needs technology, market, "
    "competition and customer economics, which the investor fills in after reading."
)
st.dataframe(pd.DataFrame([{
    "Rank": i + 1,
    "Company": d.name + ("  (yours)" if id(d) in yours else "  (sample)"),
    "Money score (1-5)": d.rank_score,
    "Numbers found": len(d.values),
    "Numbers missing": len(d.missing_metrics),
    "AI used": "yes" if d.used_llm else "no",
} for i, d in enumerate(decks)]), hide_index=True, width="stretch")

# --- One deck in detail ------------------------------------------------------
st.subheader("Check one deck")
names = [d.name for d in decks]
pick = st.selectbox("Deck", names, index=len(names) - 1 if st.session_state.uploads else 0)
d = decks[names.index(pick)]

tab_score, tab_values, tab_gaps = st.tabs(["Why this rank", "Every number and its slide", "What's missing"])

with tab_score:
    if d.financials and d.financials.grounded_formula_inputs:
        st.markdown(f"**Money score: {d.rank_score} / 5.** A weighted average of the inputs the deck gives.")
        st.code(d.financials.grounded_formula, language=None)
        st.dataframe(pd.DataFrame([{
            "Input": i["component"].replace("_", " "),
            "Value in deck": i["raw_value"],
            "Score (1-5)": i["derived_score"],
            "Weight": i["weight"],
            "Source": i["source_name"],
            "Quote": i["evidence"],
        } for i in d.financials.grounded_formula_inputs]), hide_index=True, width="stretch")
    else:
        st.warning("No money numbers found in this deck, so it ranks last.")

with tab_values:
    st.caption("Fixed rules read the deck first. An AI value is kept only if its quote appears word for word on the slide.")
    st.dataframe(pd.DataFrame([{
        "Number": METRICS[o.metric_name][0],
        "Value": o.value,
        "As of": o.as_of_date or "",
        "Slide": o.chunk_locator.replace("slide_", ""),
        "Quote": o.evidence_text,
        "Read by": "AI" if o.method == "llm" else "fixed rules",
    } for o in d.values]), hide_index=True, width="stretch")

with tab_gaps:
    if d.missing_metrics:
        st.markdown("**Missing from the deck:** " + ", ".join(METRICS[m][0] for m in d.missing_metrics if m in METRICS))
        st.text_area("Draft email asking the founder (never sent)", draft_ask(d.name, d.missing_metrics), height=220)
    else:
        st.success("Every number was found.")
    st.markdown("**Must-have scores still empty (filled after the deck):** " + ", ".join(d.missing_must_have_types))

st.divider()
st.caption(f"[Case study]({CASE_STUDY}) · [Code]({REPO}) · Sample decks are made up.")
