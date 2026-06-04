"""tools/orchestrator_tool.py — Claude in the orchestrator role (D-28).

The editor with two manuscripts. Given Claude's and GPT's independent drafts of a
section, it scores both (0–10, explicit anchors so it can't over-score — R-08/F-14),
selects the stronger base or synthesises the two, sets direction for the next
iteration, and judges whether the section is done.

Two entry points:
- `adjudicate(...)` → `(OrchestratorDecision, selected_text)`. The decision is a
  summary object (no draft text — drafts live on disk, D-07 #3); `selected_text`
  is what the loop checkpoints. For a pure claude/gpt pick the chosen draft is used
  verbatim (no rewrite drift); for "synthesis" the orchestrator returns the merged
  text it wrote (F-18).
- `read_pushbacks(...)` → revised direction string after the one-exchange writer
  pushback (D-29).

`keyword_coverage` on the decision is computed in code by the scorer (D-25),
not asked of the model. Proposed `rubric_additions` are raw here — the loop
JD-validates and caps them via tools/rubric.py.
"""

from __future__ import annotations

from tailor.helpers import claude_complete
from tailor.models import OrchestratorDecision
from tailor.tools.scorer import keyword_coverage

__all__ = ["adjudicate", "read_pushbacks", "SCORE_ANCHORS", "OrchestratorError"]


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator's structured output can't be validated after a retry."""


SELECTED_BASES = {"claude", "gpt", "synthesis"}

SCORE_ANCHORS = """\
Score each draft 0–10, calibrated — do NOT over-score:
- 9–10: only minor issues remain; submittable as-is.
- 7–8: solid, but at least one notable weakness remains.
- 5–6: a major gap; needs real work.
- 3–4: weak; would not pass a first screen for this role.
- 0–2: largely irrelevant to the role."""

_SYSTEM = f"""\
You are the orchestrator adjudicating two independent drafts of ONE CV section for \
a specific role: one by the Claude writer, one by the GPT writer. Compare them on \
fit to the JD, truthfulness, concreteness, and structure.

{SCORE_ANCHORS}

Then decide:
- selected_base: "claude" or "gpt" if one is clearly stronger; "synthesis" if the \
best version combines parts of each — and if so, WRITE the merged section in \
final_text (truthful merge only; invent nothing).
- direction: one or two sentences telling BOTH writers what to focus on next \
iteration (the single most valuable improvement). If the section is done, say so.
- converged: true only when BOTH drafts are strong AND no major issue remains — \
this is what freezes the section, so hold the bar.
- rubric_additions: up to two atomic requirements the JD clearly implies but that \
aren't in the keyword list (or [] — these are validated against the JD afterward).
Call submit_decision exactly once."""

_DECISION_TOOL = {
    "name": "submit_decision",
    "description": "Adjudicate the two drafts: score both, select or synthesise, set direction, judge convergence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_base": {"type": "string", "enum": ["claude", "gpt", "synthesis"]},
            "final_text": {"type": "string",
                           "description": "the merged section text — REQUIRED when selected_base is 'synthesis', else may be empty"},
            "direction": {"type": "string"},
            "synthesis_notes": {"type": ["string", "null"]},
            "claude_quality": {"type": "number"},
            "gpt_quality": {"type": "number"},
            "converged": {"type": "boolean"},
            "rubric_additions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["selected_base", "direction", "claude_quality", "gpt_quality", "converged"],
    },
}

_REVISE_DIR_TOOL = {
    "name": "revise_direction",
    "description": "After reading both writers' pushback, hold or revise the direction for the next iteration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "description": "the direction to carry forward (revised or unchanged)"},
            "revised": {"type": "boolean"},
            "reasoning": {"type": "string"},
        },
        "required": ["direction", "revised", "reasoning"],
    },
}


def _tool_input(resp, name):
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block.input
    return None


def _validate(data: dict) -> list[str]:
    problems = []
    if data.get("selected_base") not in SELECTED_BASES:
        problems.append(f"selected_base {data.get('selected_base')!r} invalid")
    for k in ("claude_quality", "gpt_quality"):
        v = data.get(k)
        if not isinstance(v, (int, float)) or not 0 <= v <= 10:
            problems.append(f"{k} {v!r} not in 0–10")
    if not str(data.get("direction", "")).strip():
        problems.append("direction is empty")
    if data.get("selected_base") == "synthesis" and not str(data.get("final_text", "")).strip():
        problems.append("synthesis selected but final_text is empty")
    return problems


def adjudicate(
    section_id, claude_draft, gpt_draft, rubric, jd, *,
    prior_score=None, is_final=False, model, client=None,
) -> tuple[OrchestratorDecision, str]:
    """Compare the two drafts. Returns (OrchestratorDecision, selected_text)."""
    final_note = "\nThis is the FINAL pass — make your definitive selection; no further iterations." if is_final else ""
    user = (
        f"ROLE: {jd.role_title} ({jd.seniority_level})\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"RUBRIC required keywords: {rubric.required_keywords}\n\n"
        f"--- CLAUDE DRAFT ---\n{claude_draft.text}\n\n"
        f"--- GPT DRAFT ---\n{gpt_draft.text}\n\n"
        f"Adjudicate this section.{final_note}"
    )
    data, problems = None, []
    for _ in range(2):
        resp = claude_complete(
            model=model, system=_SYSTEM, messages=[{"role": "user", "content": user}],
            tools=[_DECISION_TOOL], tool_choice={"type": "tool", "name": "submit_decision"},
            max_tokens=1500, client=client,
        )
        data = _tool_input(resp, "submit_decision")
        if data is None:
            problems = ["orchestrator did not call submit_decision"]
            continue
        problems = _validate(data)
        if not problems:
            break
    if data is None or problems:
        raise OrchestratorError(f"orchestrator decision invalid after retry ({model}): " + "; ".join(problems))

    base = data["selected_base"]
    if base == "claude":
        selected_text = claude_draft.text
    elif base == "gpt":
        selected_text = gpt_draft.text
    else:
        selected_text = data["final_text"].strip()

    decision = OrchestratorDecision(
        section_id=section_id,
        selected_base=base,
        direction=data["direction"].strip(),
        keyword_coverage=round(keyword_coverage(selected_text, rubric), 4),
        claude_quality=float(data["claude_quality"]),
        gpt_quality=float(data["gpt_quality"]),
        converged=bool(data["converged"]),
        synthesis_notes=(data.get("synthesis_notes") or None),
        rubric_additions=[str(a) for a in data.get("rubric_additions", [])],
    )
    return decision, selected_text


def read_pushbacks(section_id, decision, claude_pushback, gpt_pushback, jd, *, model, client=None) -> str:
    """One-exchange resolution (D-29): the orchestrator reads both pushbacks and
    holds or revises the direction. No pushback at all → return the direction
    unchanged (no model call)."""
    if not claude_pushback and not gpt_pushback:
        return decision.direction
    user = (
        f"ROLE: {jd.role_title}\nSECTION: {section_id}\n\n"
        f"Your direction was:\n  {decision.direction}\n\n"
        f"Claude's pushback: {claude_pushback or '(agreed)'}\n"
        f"GPT's pushback: {gpt_pushback or '(agreed)'}\n\n"
        f"Hold or revise the direction for the next iteration."
    )
    resp = claude_complete(
        model=model, system="You are the orchestrator resolving writer pushback. One decision only — do not negotiate further.",
        messages=[{"role": "user", "content": user}],
        tools=[_REVISE_DIR_TOOL], tool_choice={"type": "tool", "name": "revise_direction"},
        max_tokens=400, client=client,
    )
    data = _tool_input(resp, "revise_direction")
    if data and str(data.get("direction", "")).strip():
        return str(data["direction"]).strip()
    return decision.direction
