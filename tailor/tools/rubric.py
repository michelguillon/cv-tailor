"""tools/rubric.py — ScoringRubric update logic (D-04). SPEC §3.4, Step 6.

The rubric is a versioned first-class object. Phase 0 (Mistral) creates v1 from
the JD; the refinement loop can EXTEND it when GPT critique surfaces a requirement
the JD clearly implies but that isn't yet in the keyword list. Three safeguards
(D-04), all enforced here:

1. **Max N additions per iteration** (config `rubric.max_additions_per_iteration`,
   default 2) — without a cap, a verbose critique model could inflate the rubric
   until keyword coverage stalls, misrepresenting CV quality.
2. **Each addition is validated against the JD by the orchestrator (Claude)** —
   "is this actually implied by the JD, or is the critique model hallucinating a
   requirement?" The accept/reject decision and reasoning are logged (D-06).
3. **Provenance** — accepted additions are tracked as `RubricAddition`
   (`keyword`, `added_in_iteration`, `triggered_by`), not a flat string list.

The provider (Anthropic) is hidden behind the tool boundary (D-02). Structured
output is forced via a tool call and validated, retried once (R-09); on a hard
failure we keep the rubric unchanged rather than corrupt the scoring criteria.
"""

from __future__ import annotations

from dataclasses import replace

from tailor.audit import utc_now_iso
from tailor.helpers import claude_complete
from tailor.models import RubricAddition, ScoringRubric
from tailor.tools.scorer import normalise

__all__ = ["validate_rubric_additions"]


_VALIDATE_TOOL = {
    "name": "submit_rubric_decisions",
    "description": "Decide, per proposed keyword, whether the JD genuinely implies it as a scoring requirement.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                        "implied_by_jd": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["keyword", "implied_by_jd", "reason"],
                },
            },
        },
        "required": ["decisions"],
    },
}

_SYSTEM = """\
You guard a CV scoring rubric. A critique model has proposed new keywords to add \
to the rubric's required list. For EACH proposed keyword, decide whether the job \
description genuinely implies it as a requirement, or whether the critique model \
is inventing a requirement the JD does not actually ask for.

Accept (implied_by_jd=true) ONLY when the keyword names a capability, tool, or \
domain the JD clearly requires or strongly implies. Reject generic filler, \
nice-to-haves already covered, or requirements the JD never states. Be \
conservative — an inflated rubric makes the score meaningless. Call \
submit_rubric_decisions exactly once, with one decision per proposed keyword."""


def _known_keywords(rubric: ScoringRubric) -> set[str]:
    """Normalised set of every keyword already in the rubric (for de-dup)."""
    known = set()
    for kw in rubric.required_keywords + rubric.nice_to_have_keywords:
        known.add(normalise(kw))
    for add in rubric.added_from_critique:
        known.add(normalise(add.keyword))
    return known


def _extract_decisions(resp) -> list[dict] | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_rubric_decisions":
            return block.input.get("decisions", [])
    return None


def validate_rubric_additions(
    rubric: ScoringRubric,
    proposed: list[str],
    jd,
    iteration: int,
    *,
    model: str,
    max_additions: int = 2,
    client=None,
    audit=None,
) -> tuple[ScoringRubric, list[RubricAddition]]:
    """Validate critique-proposed keywords against the JD; extend the rubric (D-04).

    Returns ``(rubric_out, accepted)``. The rubric is returned UNCHANGED (same
    version) when nothing new is accepted — only a real extension bumps the
    version, so a version increment in the audit trail always means a real change.
    De-dups proposed keywords against everything already in the rubric first, so
    we never re-validate (or re-add) a term that's already scored.
    """
    known = _known_keywords(rubric)
    # Preserve order, drop blanks, drop duplicates (vs rubric and within `proposed`).
    candidates: list[str] = []
    seen: set[str] = set()
    for kw in proposed:
        norm = normalise(kw)
        if norm and norm not in known and norm not in seen:
            candidates.append(kw.strip())
            seen.add(norm)
    if not candidates:
        return rubric, []

    prompt = (
        f"ROLE: {jd.role_title} ({jd.seniority_level})\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"ALREADY IN RUBRIC (required): {rubric.required_keywords}\n\n"
        f"PROPOSED ADDITIONS (decide each):\n" + "\n".join(f"  - {c}" for c in candidates)
    )

    decisions = None
    for _ in range(2):
        resp = claude_complete(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_VALIDATE_TOOL],
            tool_choice={"type": "tool", "name": "submit_rubric_decisions"},
            max_tokens=800,
            client=client,
        )
        decisions = _extract_decisions(resp)
        if decisions is not None:
            break
    if decisions is None:
        # Never corrupt the rubric on a model failure — keep it unchanged (R-09).
        if audit is not None:
            audit.log_event("refinement", "rubric_validation_failed",
                            "orchestrator did not return rubric decisions; rubric left unchanged",
                            iteration=iteration, rubric_version=rubric.version)
        return rubric, []

    # Map decisions back to candidates by normalised keyword; cap to max_additions.
    verdict = {normalise(d.get("keyword", "")): d for d in decisions}
    accepted: list[RubricAddition] = []
    for cand in candidates:
        d = verdict.get(normalise(cand))
        implied = bool(d and d.get("implied_by_jd"))
        reason = (d or {}).get("reason", "no decision returned")
        if implied and len(accepted) < max_additions:
            accepted.append(RubricAddition(
                keyword=cand,
                added_in_iteration=iteration,
                triggered_by=f"GPT critique iteration {iteration} (validated: {reason})",
            ))
            if audit is not None:
                audit.log_event("refinement", "rubric_addition_accepted",
                                f"added '{cand}' to rubric — {reason}",
                                iteration=iteration, rubric_version=rubric.version + 1)
        elif audit is not None:
            why = "capped (max per iteration reached)" if implied else f"not implied by JD — {reason}"
            audit.log_event("refinement", "rubric_addition_rejected",
                            f"rejected '{cand}': {why}",
                            iteration=iteration, rubric_version=rubric.version)

    if not accepted:
        return rubric, []

    rubric_out = replace(
        rubric,
        version=rubric.version + 1,
        required_keywords=rubric.required_keywords + [a.keyword for a in accepted],
        added_from_critique=rubric.added_from_critique + accepted,
        updated_at=utc_now_iso(),
    )
    return rubric_out, accepted
