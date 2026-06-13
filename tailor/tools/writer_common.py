"""tools/writer_common.py — shared pieces for the two dual-writer tools (D-28).

Both writers (Claude, GPT) tailor a section truthfully, respect the same word
budget (D-27/F-13), self-assess with the same two severity levels (D-11), and get
the same deterministic length-budget items appended in code (D-14) — code counts
words, the model judges content. Keeping these in one place means the two writers
stay calibrated against each other, which is what makes the orchestrator's
two-draft comparison meaningful.
"""

from __future__ import annotations

import re

from tailor.candidate import CVCM_FRAMING_NOTE
from tailor.models import CritiqueItem

__all__ = [
    "SEVERITIES",
    "TRUTHFULNESS_RULES",
    "STRUCTURE_RULES",
    "SEVERITY_DEFS",
    "word_target",
    "length_items",
    "structure_preserved",
    "enforce_source_structure",
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
source doesn't show it — is FABRICATION, not coverage. When in doubt, leave it out."""

# Structure preservation is its OWN top-level rule, not a truthfulness footnote (F-56):
# buried as the last line of TRUTHFULNESS_RULES it was ignored, and the writers flattened
# bulleted experience and the "·"-delimited skills list into prose paragraphs — which made
# the rendered CV a wall of text. This block sits BEFORE the content guidance in both writer
# system prompts, and a deterministic `structure_preserved()` check (below) backs it up so the
# rule is enforced, not merely stated.
STRUCTURE_RULES = """\
STRUCTURE — match the SOURCE section's shape EXACTLY. Tailoring changes wording, never format:
- If the source is a BULLETED list (lines beginning "- "), return a bulleted list: keep each \
achievement on its own "- " line. NEVER merge the bullets into a flowing prose paragraph. You \
may reorder, sharpen, or drop a bullet, but the result must stay bullets.
- If the source is a SKILLS list (short terms separated by "·"), return the SAME "·"-delimited \
list of terms. Reorder or swap terms for relevance, but NEVER expand it into sentences — a \
skills list rewritten as prose is wrong.
- If the source is a prose paragraph, return prose. Do not invent bullets it didn't have.
Mismatching the source's structure is a defect on its own, independent of the wording quality."""

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


def _bullet_lines(text: str) -> int:
    """Count markdown bullet lines ("- "/"* ") in `text`."""
    return sum(1 for ln in text.splitlines() if ln.lstrip().startswith(("- ", "* ")))


def _is_delimited_list(text: str) -> bool:
    """True for a one-line "skills list" — short terms joined by "·"/"•" separators
    (≥2 separators ⇒ ≥3 terms). This is the skills-section shape (skills_source.md)."""
    return any(ln.count("·") >= 2 or ln.count("•") >= 2 for ln in text.splitlines())


def structure_preserved(source_text: str, draft_text: str) -> bool:
    """Deterministic check (NOT the model's self-report) that a tailored draft kept the
    SOURCE section's list shape (F-56). The writers otherwise flatten bulleted experience
    and the "·"-delimited skills list into prose, turning the rendered CV into walls of
    text. Code counts list markers, mirroring how `length_items` counts words (D-14):
      - bulleted source (≥2 bullets) → the draft must stay bulleted (≥2 bullets);
      - "·"-delimited skills list    → the draft must stay a delimited list;
      - prose source                 → no structural constraint (always True).
    The writers set this on every WriterDraft; the orchestrator treats False as a
    selection disqualifier and a freeze blocker."""
    if _bullet_lines(source_text) >= 2:
        return _bullet_lines(draft_text) >= 2
    if _is_delimited_list(source_text):
        return _is_delimited_list(draft_text)
    return True


# Sentence boundary: ".!?" + space + a capital/quote/digit start, suppressed after a single
# capital initial and the common CV abbreviations so "$5M.", "API.", "U.S." don't mis-split.
# All lookbehinds are fixed-width (Python re requirement).
_SENTENCE_SPLIT = re.compile(
    r'(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)(?<!\bInc)(?<!\bLtd)'
    r'(?<!\bNo)(?<!\bU\.S)(?<!\bU\.K)(?<=[.!?])\s+(?=[A-Z0-9"\'])'
)


def enforce_source_structure(source_text: str, draft_text: str) -> str:
    """Deterministic structure BACKSTOP (F-56). When the SOURCE is a bulleted list but the
    draft flattened it to a prose paragraph, split the prose back into bullets. The writers
    preserve the content and only drop the line breaks, so splitting on sentence boundaries
    recovers the original bullets WITHOUT changing any wording — it only inserts "- " and
    newlines, so it cannot fabricate. This guarantees bulleted experience renders as bullets
    even when BOTH writers flatten (the Haiku/demo case the prompt rule + structure_preserved
    disqualifier can't cover, since neither draft is structured to select).

    Only the reconstructable bullet case is handled. A "·"-delimited skills list flattened to
    prose can't be rebuilt deterministically (the terms are dissolved into sentences), so that
    is left to STRUCTURE_RULES + the structure_preserved disqualifier (which recover it when at
    least one writer/iteration emits the list)."""
    if _bullet_lines(source_text) >= 2 and _bullet_lines(draft_text) == 0:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(draft_text.strip()) if s.strip()]
        if len(sentences) >= 2:
            return "\n".join(f"- {s}" for s in sentences)
    return draft_text


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


def section_user_prompt(section_id, section_text, budget, direction, rejected_suggestions,
                        is_final, cvcm=None) -> str:
    """The VARIABLE half: this section's source, target length, direction, and loop
    memory. Appended after the cached prefix, so it never pollutes the cache. `cvcm`
    (optional, §3.9/D-33) is candidate value-model context for framing only."""
    target = word_target(section_text, budget)
    parts = [
        f"SECTION TYPE: {section_id}",
        f"TARGET WORDS: ~{target} (stay close to the source length; do not pad)",
    ]
    if cvcm:
        parts += ["", f"CANDIDATE VALUE MODEL (frame the candidate's real content through these "
                      f"recurring value patterns). {CVCM_FRAMING_NOTE}", cvcm]
    if direction:
        parts += ["", f"ORCHESTRATOR'S DIRECTION FOR THIS SECTION: {direction}"]
    if rejected_suggestions:
        parts += ["", "ALREADY CONSIDERED — do not just re-raise these:",
                  *(f"  - {s}" for s in rejected_suggestions)]
    if is_final:
        parts += ["", "This is the FINAL pass — produce your definitive version."]
    parts += ["", f"SOURCE SECTION:\n{section_text}"]
    return "\n".join(parts)
