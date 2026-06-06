"""tailor/candidate.py — load the optional Candidate Value Creation Model (CVCM).

SPEC §3.9 / D-33. The CVCM is a durable markdown file the *candidate* authors and
maintains (`candidate/value_creation_model.md`) describing how they consistently create
value — recurring patterns independent of titles, industries, or specific achievements.

It is OPTIONAL context, not a fact source. The system reads it; it never writes it. The
pipeline runs identically without it. When present it shifts tailoring from keyword
optimisation toward articulating authentic value — but it changes EMPHASIS and NARRATIVE
only: the CV corpus remains the single source of factual claims (F-34/F-35), so a claim
that appears only in the CVCM and not in the candidate's CV is still flagged by the
verification gate. The loaded text is threaded into Phases 1, 2, and 3 as candidate context.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["load_cvcm", "CVCM_PATH", "CVCM_FRAMING_NOTE"]

CVCM_PATH = Path("candidate/value_creation_model.md")

# The governing rule, embedded in every prompt the CVCM reaches: it guides how real CV
# content is framed; it never licenses a new claim. Keeps the trust gate meaningful (D-33).
# Forceful by necessity: a weaker note let writers lift value-model phrasing straight into
# the CV (F-36) — the model is BACKGROUND that reorders/reframes real content, never text.
CVCM_FRAMING_NOTE = (
    "The value model is BACKGROUND ONLY. It tells you which of the candidate's REAL, "
    "already-present achievements to lead with and how to frame them — nothing more. NEVER "
    "copy or paraphrase any wording from the value model into the CV, and never turn it into a "
    "bullet, a skill, or a claim. It is NOT a source of facts: if a concept from the value "
    "model is not already evidenced in this section's SOURCE, it does NOT appear in the output. "
    "The source section is the only content; the value model only reorders and reframes it."
)


def load_cvcm(path: str | Path = CVCM_PATH) -> str | None:
    """Return the candidate's value-creation-model text, or None if the file is absent
    or empty. Never raises on a missing file — the CVCM is optional (D-33)."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None
