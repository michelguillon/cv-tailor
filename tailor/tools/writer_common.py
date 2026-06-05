"""tools/writer_common.py — shared pieces for the two dual-writer tools (D-28).

Both writers (Claude, GPT) tailor a section truthfully, respect the same word
budget (D-27/F-13), self-assess with the same two severity levels (D-11), and get
the same deterministic length-budget items appended in code (D-14) — code counts
words, the model judges content. Keeping these in one place means the two writers
stay calibrated against each other, which is what makes the orchestrator's
two-draft comparison meaningful.
"""

from __future__ import annotations

from tailor.models import CritiqueItem

__all__ = [
    "SEVERITIES",
    "TRUTHFULNESS_RULES",
    "SEVERITY_DEFS",
    "word_target",
    "length_items",
    "jd_rubric_block",
    "section_user_prompt",
]

SEVERITIES = {"major", "minor"}

# A section materially below budget if under this fraction of min_words (matches
# the old critique tool's threshold so length calls don't shift under the rewrite).
_UNDER_BUDGET_FRACTION = 0.7

TRUTHFULNESS_RULES = """\
HARD RULES — a CV that fabricates is worse than useless. Tailoring means SELECTING and \
RE-EMPHASISING the candidate's real content for this role; it is NEVER inventing or \
re-labelling it. Every claim must be traceable to the SOURCE section below.
- Do NOT invent or alter employers, job titles, dates, companies, metrics, or facts.
- Do NOT assert a job identity, role, or seniority the source doesn't state, and do NOT \
add a headline, title, or positioning tagline to a section (e.g. never open a section with \
"Solutions Engineering and Pre-Sales Leader — …"). Describe what was actually done; the \
real role title is handled separately and must not be restated or reframed in the body.
- Do NOT claim an industry, sector, or domain the source doesn't state — e.g. do not \
recast adtech, identity, ad-platform, or semiconductor work as "fintech", "payments", or \
"financial services". The candidate's actual domains are the only ones you may name.
- Use a JD or rubric keyword ONLY where the source already demonstrates that exact thing, \
in your own grounded words. Inserting an unsupported term — or bolting a JD phrase onto a \
real achievement ("…through executive communication with C-level stakeholders") when the \
source doesn't show it — is FABRICATION, not coverage. When in doubt, leave it out.
- Preserve the source's structure: if it uses bullet points, return bullet points."""

SEVERITY_DEFS = """\
When you self-assess, flag issues with exactly two severity levels:
- "major": materially weakens the application or contradicts a JD requirement \
(a required capability is absent or buried, a claim is vague where the JD wants \
evidence). The loop only freezes a section when zero major issues remain, so be \
honest — a real gap is major.
- "minor": an improvement opportunity; the section is acceptable for submission \
without it."""


def word_target(section_text: str, budget) -> int:
    """Draft target = clamp(source length, min, max) — anchored to source, not the
    corpus median (D-27/F-13: padding a terse section toward a median is fabrication
    risk)."""
    wc = len(section_text.split())
    if budget:
        return min(max(wc, budget.min_words), budget.max_words)
    return wc or 120


def length_items(section_id: str, text: str, budget, writer: str) -> list[CritiqueItem]:
    """Deterministic length-budget items (D-14) for one writer's own draft — code
    counts words, the model judges content. Applied to BOTH writers so a length
    violation reaches the orchestrator's zero-major check regardless of who wrote
    the over/under-length draft (F-17)."""
    if not budget:
        return []
    wc = len(text.split())
    if wc > budget.max_words:
        return [CritiqueItem(
            section=section_id, severity="major",
            issue=f"Section is {wc} words, over the {budget.max_words}-word budget (breaks the two-page limit).",
            suggestion="Cut to the most role-relevant points.",
            source_writer=writer,
        )]
    if wc < budget.min_words * _UNDER_BUDGET_FRACTION:
        return [CritiqueItem(
            section=section_id, severity="minor",
            issue=f"Section is {wc} words, well below the {budget.min_words}-word norm (undertells the role).",
            suggestion="Add concrete, role-relevant detail.",
            source_writer=writer,
        )]
    return []


def jd_rubric_block(jd, rubric) -> str:
    """The STABLE half of a writer/orchestrator prompt (role + JD requirements +
    rubric). Identical across every section within an iteration, so it's the cache
    prefix (D-31): a writer call for section 8 reuses section 1's cached block. A
    rubric extension changes this block (and only this block) — the system prompt
    cache above it still hits."""
    return (
        f"ROLE BEING TARGETED: {jd.role_title} ({jd.seniority_level})\n"
        f"WHAT THIS JD VALUES (priorities to surface ONLY where the source genuinely "
        f"supports them):\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"JD-RELEVANT TERMS — these are a relevance guide, NOT a checklist to insert. Use a "
        f"term only where the source already evidences it, in your own words; an unsupported "
        f"term is fabrication, not coverage:\n"
        f"  priority: {rubric.required_keywords}\n"
        f"  nice-to-have: {rubric.nice_to_have_keywords}"
    )


def section_user_prompt(section_id, section_text, budget, direction, rejected_suggestions, is_final) -> str:
    """The VARIABLE half: this section's source, target length, direction, and loop
    memory. Appended after the cached prefix, so it never pollutes the cache."""
    target = word_target(section_text, budget)
    parts = [
        f"SECTION TYPE: {section_id}",
        f"TARGET WORDS: ~{target} (stay close to the source length; do not pad)",
    ]
    if direction:
        parts += ["", f"ORCHESTRATOR'S DIRECTION FOR THIS SECTION: {direction}"]
    if rejected_suggestions:
        parts += ["", "ALREADY CONSIDERED — do not just re-raise these:",
                  *(f"  - {s}" for s in rejected_suggestions)]
    if is_final:
        parts += ["", "This is the FINAL pass — produce your definitive version."]
    parts += ["", f"SOURCE SECTION:\n{section_text}"]
    return "\n".join(parts)
