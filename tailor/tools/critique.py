"""tools/critique.py — section-by-section critique (GPT-4o-mini). SPEC Step 5.

The orchestrator calls this like any tool; the provider (OpenAI) is hidden (D-02).
GPT-4o-mini is the deliberate choice (D-03): empirically harsher, more direct CV
feedback than Claude, and an independent second opinion from a different training
distribution.

Two design requirements baked into the prompt:
- **Severity is defined in the prompt, not just the schema (D-11):** the soft-stop
  depends on "zero major items", so `major`/`minor` must mean the same thing every
  iteration. The strict JSON schema also enums severity so the model *cannot* emit
  anything else.
- **Score anchors (R-08):** without explicit "9 = …, 7 = …, 5 = …" anchors an
  LLM-judge over-scores everything 8+, flattening the convergence signal.

Length violations are appended **deterministically** (not left to the model to
count words): major if a section exceeds its max_words budget, minor if it's
materially below min_words (D-14). Code checks lengths; GPT judges content.

Output is validated before use (R-09): overall_score in 0–10, every severity in
{major, minor}, every referenced section a real section_id. Retry once, then raise.
"""

from __future__ import annotations

import json

from tailor.helpers import gpt_complete
from tailor.models import Critique, CritiqueItem

__all__ = ["critique_sections", "CritiqueError"]

SEVERITIES = {"major", "minor"}
# A section materially below budget if under this fraction of min_words.
_UNDER_BUDGET_FRACTION = 0.7


class CritiqueError(RuntimeError):
    """Raised when GPT critique output can't be validated after a retry (R-09)."""


_SYSTEM = """\
You are a demanding CV reviewer assessing how well each section supports a \
specific job application. Be harsh and specific — flattering feedback is useless. \
Critique ONLY the sections provided; reference each item by its exact section id.

SEVERITY (use exactly these two levels):
- "major": the issue materially weakens the application or contradicts a JD \
requirement (e.g. a required capability is absent or buried, a claim is vague \
where the JD demands evidence, the section omits something the role hinges on).
- "minor": an improvement opportunity; the section is acceptable for submission \
without it.

OVERALL SCORE (0–10), calibrated — do not over-score:
- 9–10: only minor issues remain; submittable as-is.
- 7–8: solid, but at least one section still has a notable weakness.
- 5–6: multiple sections have major gaps; needs real work.
- 3–4: weak; would not pass a first screen for this role.
- 0–2: largely irrelevant to the role.

Also return a per-section score (0–10) and any rubric_additions: requirements the \
JD clearly implies but that aren't in the provided keyword list (atomic terms)."""

# Strict structured output. section_scores is a LIST (not a dict) so strict mode
# can enforce additionalProperties:false; converted to a dict after parsing.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "critique",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "overall_score": {"type": "number"},
                "section_scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "section": {"type": "string"},
                            "score": {"type": "number"},
                        },
                        "required": ["section", "score"],
                    },
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "section": {"type": "string"},
                            "severity": {"type": "string", "enum": ["major", "minor"]},
                            "issue": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["section", "severity", "issue", "suggestion"],
                    },
                },
                "rubric_additions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["overall_score", "section_scores", "items", "rubric_additions"],
        },
    },
}


def _build_prompt(sections: dict[str, str], jd, rubric) -> str:
    blocks = "\n\n".join(f"### SECTION: {sid}\n{text}" for sid, text in sections.items())
    return (
        f"ROLE: {jd.role_title} ({jd.seniority_level})\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"RUBRIC required keywords: {rubric.required_keywords}\n"
        f"RUBRIC nice-to-have: {rubric.nice_to_have_keywords}\n\n"
        f"Critique each section below for this role.\n\n{blocks}"
    )


def _validate(data: dict, valid_sections: set[str]) -> list[str]:
    problems = []
    score = data.get("overall_score")
    if not isinstance(score, (int, float)) or not 0 <= score <= 10:
        problems.append(f"overall_score {score!r} not in 0–10")
    for i, it in enumerate(data.get("items", [])):
        if it.get("severity") not in SEVERITIES:
            problems.append(f"items[{i}].severity {it.get('severity')!r} invalid")
        if it.get("section") not in valid_sections:
            problems.append(f"items[{i}].section {it.get('section')!r} not a provided section")
    for i, ss in enumerate(data.get("section_scores", [])):
        s = ss.get("score")
        if not isinstance(s, (int, float)) or not 0 <= s <= 10:
            problems.append(f"section_scores[{i}].score {s!r} not in 0–10")
    return problems


def _length_items(sections: dict[str, str], budgets, section_types: dict[str, str]) -> list[CritiqueItem]:
    """Deterministic length-budget CritiqueItems (D-14) — code counts, not GPT."""
    out = []
    for sid, text in sections.items():
        budget = budgets.get(section_types.get(sid, ""))
        if not budget:
            continue
        wc = len(text.split())
        if wc > budget.max_words:
            out.append(CritiqueItem(
                section=sid, severity="major",
                issue=f"Section is {wc} words, over the {budget.max_words}-word budget (breaks the two-page limit).",
                suggestion="Cut to the most role-relevant points.",
            ))
        elif wc < budget.min_words * _UNDER_BUDGET_FRACTION:
            out.append(CritiqueItem(
                section=sid, severity="minor",
                issue=f"Section is {wc} words, well below the {budget.min_words}-word norm (undertells the role).",
                suggestion="Add concrete, role-relevant detail.",
            ))
    return out


def critique_sections(
    sections: dict[str, str],
    jd,
    rubric,
    *,
    model: str = "gpt-4o-mini",
    budgets: dict | None = None,
    section_types: dict[str, str] | None = None,
    client=None,
) -> Critique:
    """Critique the given active sections. Returns a validated Critique.

    `sections` maps section_id → current draft text. `section_types` maps
    section_id → section_type (for length budgets); optional.
    """
    if not sections:
        return Critique(overall_score=10.0)  # nothing active to critique

    valid = set(sections)
    prompt = _build_prompt(sections, jd, rubric)

    last_problems, data = [], None
    for _ in range(2):
        resp = gpt_complete(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
            response_format=_RESPONSE_FORMAT,
            max_tokens=2000,
            client=client,
        )
        try:
            data = json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, AttributeError, IndexError) as exc:
            last_problems = [f"unparseable response: {exc}"]
            continue
        last_problems = _validate(data, valid)
        if not last_problems:
            break
    if data is None or last_problems:
        raise CritiqueError(f"critique invalid after retry ({model}): " + "; ".join(last_problems))

    items = [
        CritiqueItem(section=it["section"], severity=it["severity"],
                     issue=it["issue"], suggestion=it["suggestion"])
        for it in data["items"]
    ]
    if budgets and section_types:
        items.extend(_length_items(sections, budgets, section_types))

    return Critique(
        overall_score=float(data["overall_score"]),
        section_scores={ss["section"]: float(ss["score"]) for ss in data["section_scores"]},
        items=items,
        rubric_additions=[str(a) for a in data["rubric_additions"]],
    )
