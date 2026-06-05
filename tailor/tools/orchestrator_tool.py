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

from tailor.helpers import cached, claude_complete
from tailor.models import OrchestratorDecision
from tailor.tools.scorer import keyword_coverage
from tailor.tools.writer_common import jd_rubric_block

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
a specific role: one by the Claude writer, one by the GPT writer. You are given the \
SOURCE section both writers tailored from. Judge TRUTHFULNESS first, then fit to the \
JD, concreteness, and structure.

GROUNDING (this overrides everything else): every claim in a draft must be supported by \
the SOURCE. Treat as FABRICATION — and a major problem — any of: an invented or altered \
title, metric, or date; an asserted job identity, seniority, headline, or tagline the \
source doesn't state (e.g. opening with "Solutions Engineering and Pre-Sales Leader — "); \
a claimed industry/sector/domain not in the source (e.g. calling non-fintech work \
"fintech"/"payments"/"financial services"); or a JD/rubric keyword inserted where the \
source gives no evidence. A draft that fabricates scores at most 4/10 however well it \
fits the JD, must NOT be marked converged, and its direction must be to CUT the \
unsupported content.

{SCORE_ANCHORS}

Then decide:
- selected_base: "claude" or "gpt" if one is clearly stronger — prefer the more faithful \
draft; "synthesis" if the best version combines parts of each — and if so, WRITE the \
merged section in final_text using ONLY source-supported content (invent nothing).
- direction: one or two sentences telling BOTH writers the single most valuable \
improvement for next iteration; if a draft fabricated, the direction is to remove the \
unsupported claim(s). If the section is done, say so.
- converged: true ONLY when both drafts are strong, fully grounded in the source (no \
unsupported claim, identity, sector, or keyword), AND no major issue remains — this \
freezes the section, so hold the bar.
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
    source_text="", prior_score=None, is_final=False, model, client=None,
) -> tuple[OrchestratorDecision, str]:
    """Compare the two drafts. Returns (OrchestratorDecision, selected_text).

    `source_text` is the section both writers tailored from — the ground truth the
    orchestrator checks each draft against to catch fabrication (Fix C). It must NOT
    join the cache prefix (it varies per section), so it lives in the user message."""
    final_note = "\nThis is the FINAL pass — make your definitive selection; no further iterations." if is_final else ""
    # Cached stable prefix (system + role/JD/rubric); the source + two drafts vary (D-31).
    system = [cached(_SYSTEM), cached(jd_rubric_block(jd, rubric))]
    user = (
        f"SOURCE SECTION (ground truth — every claim in a draft must trace to here):\n"
        f"{source_text or '(source unavailable — judge truthfulness conservatively)'}\n\n"
        f"--- CLAUDE DRAFT ---\n{claude_draft.text}\n\n"
        f"--- GPT DRAFT ---\n{gpt_draft.text}\n\n"
        f"Adjudicate this section.{final_note}"
    )
    data, problems = None, []
    for _ in range(2):
        resp = claude_complete(
            model=model, system=system, messages=[{"role": "user", "content": user}],
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
