"""tools/gpt_writer.py — GPT-4o-mini in the writer role (D-28).

The second, independent drafter. GPT is the harsher, bolder writer (D-03):
empirically more direct, less flattering — a genuinely different prior from
Claude, which is the whole point of two writers. Same interface as
tools/claude_writer.py so the loop treats them symmetrically.

OpenAI strict `json_schema` enforces the severity enum server-side, so a bad
severity can never reach the soft-stop/freeze logic. Length-budget items are
appended deterministically in code (D-14). Output validated; retry once (R-09).
"""

from __future__ import annotations

import json

from tailor.helpers import gpt_complete
from tailor.models import CritiqueItem, WriterDraft
from tailor.tools.writer_common import (
    SEVERITIES,
    SEVERITY_DEFS,
    STRUCTURE_RULES,
    TRUTHFULNESS_RULES,
    jd_rubric_block,
    length_items,
    section_user_prompt,
    structure_preserved,
)

__all__ = ["write_section", "pushback", "WriterError"]

WRITER = "gpt"


class WriterError(RuntimeError):
    """Raised when GPT's structured output can't be validated after a retry."""


_SYSTEM = f"""\
You are a demanding, direct CV writer. You tailor ONE section to a specific role \
and you do not flatter — if the source undersells a real strength you sharpen it, \
and you cut anything that doesn't earn its place for THIS job.

{STRUCTURE_RULES}

Within that structure: be bolder and more concrete than a cautious writer would.

{TRUTHFULNESS_RULES}

{SEVERITY_DEFS}

If given the orchestrator's DIRECTION, follow it unless it would force a \
fabrication or weaken a true claim. Return your tailored text and an honest \
self-assessment of any issues that remain in YOUR draft."""

_DRAFT_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "writer_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
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
    },
}

_PUSHBACK_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "pushback",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "disagree": {"type": "boolean"},
                "reasoning": {"type": "string"},
            },
            "required": ["disagree", "reasoning"],
        },
    },
}


def _parse(resp):
    try:
        return json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, AttributeError, IndexError):
        return None


def write_section(
    section_id, section_text, jd, rubric, budget, *,
    version, direction=None, rejected_suggestions=(), is_final=False,
    model="gpt-4o-mini", client=None, cvcm=None,
) -> WriterDraft:
    """Draft one section as GPT. Returns a validated WriterDraft (pushback=None)."""
    # Stable system (instructions + role/JD/rubric) first so OpenAI auto-caches the
    # prefix across sections; variable section in the user message (D-31).
    system = f"{_SYSTEM}\n\n{jd_rubric_block(jd, rubric)}"
    user = section_user_prompt(section_id, section_text, budget,
                               direction, rejected_suggestions, is_final, cvcm=cvcm)
    data = None
    for _ in range(2):
        resp = gpt_complete(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=_DRAFT_FORMAT, max_tokens=max(512, len(section_text.split()) * 8),
            client=client,
        )
        data = _parse(resp)
        if data and isinstance(data.get("text"), str) and data["text"].strip() \
                and all(it.get("severity") in SEVERITIES for it in data.get("items", [])):
            break
        data = None
    if data is None:
        raise WriterError(f"gpt_writer produced no valid draft for {section_id} ({model})")

    text = data["text"].strip()
    items = [CritiqueItem(section=section_id, severity=it["severity"], issue=it["issue"],
                          suggestion=it["suggestion"], source_writer=WRITER)
             for it in (data.get("items") or [])]
    items += length_items(section_id, text, budget, WRITER)
    # Deterministic structure check — counts source vs draft list markers, never trusts the
    # model to self-report it (F-56). The orchestrator disqualifies a flattened draft.
    return WriterDraft(writer=WRITER, section_id=section_id, text=text,
                       version=version, pushback=None, items=items,
                       structure_preserved=structure_preserved(section_text, text))


def pushback(section_id, decision, my_draft, jd, *, model="gpt-4o-mini", client=None) -> str | None:
    """GPT reads the orchestrator's decision/direction and may object once (D-29)."""
    user = (
        f"ROLE: {jd.role_title}\nSECTION: {section_id}\n\n"
        f"The orchestrator chose '{decision.selected_base}' as the base "
        f"(your draft scored {decision.gpt_quality}/10) and set this direction:\n  {decision.direction}\n\n"
        f"YOUR current draft was:\n{my_draft.text}\n\n"
        f"If this direction would weaken the section or push toward fabrication, set disagree=true "
        f"and explain. Otherwise disagree=false."
    )
    resp = gpt_complete(
        model=model,
        messages=[{"role": "system", "content": "You are the GPT writer reviewing the orchestrator's direction. Be honest and brief."},
                  {"role": "user", "content": user}],
        response_format=_PUSHBACK_FORMAT, max_tokens=300, client=client,
    )
    data = _parse(resp)
    if data and data.get("disagree") and str(data.get("reasoning", "")).strip():
        return str(data["reasoning"]).strip()
    return None
