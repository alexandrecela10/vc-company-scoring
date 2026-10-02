"""Generate three fictional pitch decks for the public demo.

Why: the demo needs sample decks a visitor can rank against, and the real
sample deck contains a real company name. These decks are fully fictional.

Run once, commit the PDFs:  venv/bin/python -m demo.make_sample_decks
"""
from pathlib import Path

from fpdf import FPDF

OUT = Path(__file__).resolve().parent / "samples"

# One entry per deck: file name -> list of slides (title, body lines).
# Numbers differ on purpose so the three decks rank in a clear order.
DECKS = {
    "fintech_a_series_a.pdf": [
        ("Fintech A", ["B2B payments for small businesses", "Series A deck, April 2026"]),
        ("Team", ["Founded in 2021", "CEO previously founded and sold a payments startup (acquired 2019)",
                  "CTO and technical co-founder, ex-bank engineering lead", "Team of 38 employees"]),
        ("Traction", ["ARR: $4.2M as of April 2026", "62 paying customers", "Revenue 2025: $1.8M"]),
        ("Financials", ["FY2025 actuals", "Gross margin: 71%", "Monthly burn: $150K", "Runway: 24 months"]),
        ("The raise", ["Raising a $14M Series A"]),
    ],
    "healthtech_b_seed.pdf": [
        ("Healthtech B", ["Clinical note automation for clinics", "Seed deck, March 2026"]),
        ("Team", ["Founded in 2023", "Team of 12 employees"]),
        ("Traction", ["ARR: $0.6M as of March 2026", "14 paying customers", "Revenue 2025: $0.3M"]),
        ("Financials", ["FY2025 actuals", "Gross margin: 55%", "Monthly burn: $120K", "Runway: 11 months"]),
        ("The raise", ["Raising a $3M seed round"]),
    ],
    "climate_c_pre_seed.pdf": [
        ("Climate C", ["Battery analytics for grid operators", "Pre-seed deck, May 2026"]),
        ("Team", ["Founded in 2025", "Team of 5 employees"]),
        ("Traction", ["3 pilot customers"]),
        ("The raise", ["Raising a $1M pre-seed round"]),
    ],
}


def build(path: Path, slides) -> None:
    """Write one landscape PDF, one page per slide."""
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(False)
    for title, lines in slides:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 30)
        pdf.cell(0, 30, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 18)
        for line in lines:
            pdf.cell(0, 14, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, slides in DECKS.items():
        build(OUT / name, slides)
        print("wrote", OUT / name)
