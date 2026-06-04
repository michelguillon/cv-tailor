"""tools/claude_writer.py — Claude in the writer role (D-28).

One of two independent drafters in the refinement loop. Claude is the precise,
evidence-led writer (its prior); GPT (tools/gpt_writer.py) is the harsher, bolder
one (D-03). Each drafts every active section independently; the orchestrator
(tools/orchestrator_tool.py) compares the two.

Two entry points:
- `write_section(...)` → a `WriterDraft` (text + self-assessed CritiqueItems).
- `pushback(...)`     → `str | None`: the writer's objection to the orchestrator's
  direction, or None if it agrees (one exchange only, D-29).

Structured output is forced via a tool call and validated; retry once, then raise
(R-09). The provider (Anthropic) is hidden behind helpers.claude_complete (D-02).
"""

from __future__ import annotations

from tailor.helpers import claude_complete
from tailor.models import CritiqueItem, WriterDraft
from tailor.tools.writer_common import (
    SEVERITIES,
    SEVERITY_DEFS,
    TRUTHFULNESS_RULES,
    build_writer_user_prompt,
    length_items,
)

__all__ = ["write_section", "pushback", "WriterError"]

WRITER = "claude"


class WriterError(RuntimeError):
    """Raised when a writer's structured output can't be validated after a retry."""


_SYSTEM = f"""\
You are a precise, evidence-led CV writer. You tailor ONE section to a specific \
role so it surfaces what matters for THIS job — truthfully. Lead with the \
strongest real evidence; cut filler; make every line earn its place.

{TRUTHFULNESS_RULES}

{SEVERITY_DEFS}

If given the orchestrator's DIRECTION, follow it unless doing so would force a \
fabrication or weaken a true claim. Call submit_draft exactly once with your \
tailored text and an honest self-assessment of any issues that remain in YOUR \
draft."""

_DRAFT_TOOL = {
    "name": "submit_draft",
    "description": "Return your tailored section text and an honest self-assessment of its remaining issues.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "the tailored section (markdown, no heading)"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["major", "minor"]},
                        "issue": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["severity", "issue", "suggestion"],
                },
            },
        },
        "required": ["text", "items"],
    },
}

_PUSHBACK_TOOL = {
    "name": "submit_pushback",
    "description": "State whether you disagree with the orchestrator's direction for this section, and why.",
    "input_schema": {
        "type": "object",
        "properties": {
            "disagree": {"type": "boolean"},
            "reasoning": {"type": "string", "description": "why the direction would weaken the section; empty if you agree"},
        },
        "required": ["disagree", "reasoning"],
    },
}


def _tool_input(resp, name):
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block.input
    return None


def write_section(
    section_id, section_text, jd, rubric, budget, *,
    version, direction=None, rejected_suggestions=(), is_final=False,
    model, client=None,
) -> WriterDraft:
    """Draft one section as Claude. Returns a validated WriterDraft (pushback=None;
    pushback is a separate exchange). `version` mirrors the iteration number."""
    user = build_writer_user_prompt(section_id, section_text, jd, rubric, budget,
                                    direction, rejected_suggestions, is_final)
    data = None
    for _ in range(2):
        resp = claude_complete(
            model=model, system=_SYSTEM, messages=[{"role": "user", "content": user}],
            tools=[_DRAFT_TOOL], tool_choice={"type": "tool", "name": "submit_draft"},
            max_tokens=max(512, len(section_text.split()) * 8), client=client,
        )
        data = _tool_input(resp, "submit_draft")
        if data and isinstance(data.get("text"), str) and data["text"].strip() \
                and all(it.get("severity") in SEVERITIES for it in data.get("items", [])):
            break
        data = None
    if data is None:
        raise WriterError(f"claude_writer produced no valid draft for {section_id} ({model})")

    items = [CritiqueItem(section=section_id, severity=it["severity"], issue=it["issue"],
                          suggestion=it["suggestion"], source_writer=WRITER)
             for it in (data.get("items") or [])]
    items += length_items(section_id, data["text"], budget, WRITER)
    return WriterDraft(writer=WRITER, section_id=section_id, text=data["text"].strip(),
                       version=version, pushback=None, items=items)


def pushback(section_id, decision, my_draft, jd, *, model, client=None) -> str | None:
    """Claude reads the orchestrator's decision/direction and may object once (D-29).
    Returns the objection text, or None if it agrees."""
    user = (
        f"ROLE: {jd.role_title}\nSECTION: {section_id}\n\n"
        f"The orchestrator chose '{decision.selected_base}' as the base "
        f"(your draft scored {decision.claude_quality}/10) and set this direction "
        f"for the next pass:\n  {decision.direction}\n\n"
        f"YOUR current draft was:\n{my_draft.text}\n\n"
        f"If this direction would weaken the section or push toward fabrication, say so. "
        f"Otherwise agree."
    )
    resp = claude_complete(
        model=model, system="You are the Claude writer reviewing the orchestrator's direction. Be honest and brief.",
        messages=[{"role": "user", "content": user}],
        tools=[_PUSHBACK_TOOL], tool_choice={"type": "tool", "name": "submit_pushback"},
        max_tokens=400, client=client,
    )
    data = _tool_input(resp, "submit_pushback")
    if data and data.get("disagree") and str(data.get("reasoning", "")).strip():
        return str(data["reasoning"]).strip()
    return None
