"""Phase 1 — Fit assessment (RAG composition + Claude reasoning). SPEC §5 Phase 1.

Input:  JDAnalysis, ScoringRubric, the ingested corpus (ChromaDB).
Output: FitAssessment (outcome, fit score, transferable skills, typed gaps,
        recommended section mix). `no_fit` stops the pipeline (D-16).
Models: Mistral embeddings (ingestion) + Claude (Haiku in dev / Sonnet in full).

Hybrid by design (D-01): the deterministic layer measures and composes (which CV
variant covers each section best — section-level mix, D-17), Claude judges what
the numbers can't — gap *type* and severity, transferability, the overall
outcome. Keyword coverage feeds selection and the rubric, never the verdict (F-11).

Experience is mixed per *company*, not per role-group, because role-group
section_ids don't align across CV variants (F-11); each company's sections are
taken from the CV that covers that company best.
"""

from __future__ import annotations

import json
from collections import defaultdict

from corpus.retrieval import all_sections
from tailor.helpers import claude_complete
from tailor.models import FitAssessment, FitGap, SectionRecommendation
from tailor.tools.scorer import coverage_report, keyword_coverage, normalise, union_coverage

__all__ = ["assess_fit", "build_composition", "render_fit_hitl", "FitAssessmentError"]

GAP_TYPES = {"keyword", "experience", "hard_requirement", "seniority"}
SEVERITIES = {"minor", "major", "blocking"}
OUTCOMES = {"strong", "partial", "no_fit"}


class FitAssessmentError(RuntimeError):
    """Raised when Claude's fit output can't be validated after a retry (R-09)."""


# --------------------------------------------------------------------------- #
# Deterministic composition                                                   #
# --------------------------------------------------------------------------- #

def _company_key(name: str) -> str:
    """Group company-name variants: 'Imagination Technologies, PowerVR Graphics'
    and 'Imagination Technologies' → same key."""
    return normalise(name.split(",")[0])


def _short_cv(filename: str) -> str:
    return filename.replace("CV_Michel_Guillon_2026_", "").replace(".docx", "")


def build_composition(sections: list[dict], rubric, static_sections) -> tuple[dict, dict]:
    """Pick the best source section per section (section-level mix, D-17).

    Returns (recommended_sections: dict[section_id → SectionRecommendation],
    diagnostics: dict). Non-experience non-static sections pick the best variant
    per section_type; experience picks the best CV per company; static sections
    come from the primary base CV (best overall coverage).
    """
    static = set(static_sections)
    by_cv: dict[str, list[dict]] = defaultdict(list)
    for s in sections:
        by_cv[s["filename"]].append(s)
    overall = {
        cv: union_coverage([s["document"] for s in ss if not s["static"]], rubric)
        for cv, ss in by_cv.items()
    }
    primary_base = max(overall, key=overall.get)

    recommended: dict[str, SectionRecommendation] = {}

    def rec(s, reason):
        return SectionRecommendation(
            section_id=s["section_id"],
            source_cv=_short_cv(s["filename"]),
            section_version=str(s.get("version_date", "")),
            keyword_coverage=round(keyword_coverage(s["document"], rubric), 3),
            reason=reason,
        )

    # Non-static, non-experience single types → best-covering variant.
    by_type: dict[str, list[dict]] = defaultdict(list)
    for s in sections:
        by_type[s["section_type"]].append(s)
    for st, cands in by_type.items():
        if st == "experience" or st in static:
            continue
        best = max(cands, key=lambda s: keyword_coverage(s["document"], rubric))
        recommended[best["section_id"]] = rec(best, f"best {st} coverage")

    # Experience → per company, the CV that covers that company best.
    exp = [s for s in sections if s["section_type"] == "experience"]
    by_company: dict[str, list[dict]] = defaultdict(list)
    for s in exp:
        by_company[_company_key(s.get("company", ""))].append(s)
    for css in by_company.values():
        cv_groups: dict[str, list[dict]] = defaultdict(list)
        for s in css:
            cv_groups[s["filename"]].append(s)
        best_cv = max(
            cv_groups,
            key=lambda cv: union_coverage([s["document"] for s in cv_groups[cv]], rubric),
        )
        label = next((s.get("company") for s in css if s.get("company")), "experience")
        for s in cv_groups[best_cv]:
            recommended[s["section_id"]] = rec(s, f"best '{label}' experience")

    # Static sections → from the primary base CV (editorially fixed; source is moot).
    for s in by_cv[primary_base]:
        if s["static"]:
            recommended[s["section_id"]] = rec(s, "static — from primary base CV")

    composed_nonstatic = [
        s["document"]
        for s in sections
        if s["section_id"] in recommended and not s["static"]
    ]
    report = coverage_report(" \n ".join(composed_nonstatic), rubric)
    diagnostics = {
        "overall_by_cv": {_short_cv(k): round(v, 3) for k, v in sorted(overall.items(), key=lambda kv: -kv[1])},
        "primary_base": _short_cv(primary_base),
        "composed_coverage": round(union_coverage(composed_nonstatic, rubric), 3),
        "matched_keywords": report.matched,
        "missing_keywords": report.missing,
    }
    return recommended, diagnostics


# --------------------------------------------------------------------------- #
# Claude reasoning (typed gaps + outcome)                                     #
# --------------------------------------------------------------------------- #

_FIT_TOOL = {
    "name": "submit_fit_assessment",
    "description": "Return the fit assessment for this JD given the candidate's best composed CV sections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": sorted(OUTCOMES)},
            "skills_transferable": {"type": "array", "items": {"type": "string"}},
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement": {"type": "string"},
                        "gap_type": {"type": "string", "enum": sorted(GAP_TYPES)},
                        "addressable": {"type": "boolean"},
                        "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                        "reason": {"type": "string"},
                    },
                    "required": ["requirement", "gap_type", "addressable", "severity", "reason"],
                },
            },
            "no_fit_reason": {"type": ["string", "null"]},
        },
        "required": ["outcome", "skills_transferable", "gaps"],
    },
}

_SYSTEM = """\
You assess whether a candidate should apply for a role, given their best CV \
content and the job's requirements. Be honest, not flattering (a fit assessment \
that always says "apply" is useless).

Gap types (D-16): "keyword" = a term/skill missing but easily added by tailoring; \
"experience" = a capability partly evidenced, addressable by reframing; \
"hard_requirement" = a credential/clearance/qualification that cannot be tailored \
in (missing → blocking); "seniority" = a level mismatch.

Severity: "blocking" = cannot apply credibly; "major" = materially weakens; \
"minor" = improvement opportunity.

Outcomes: "no_fit" = at least one blocking hard_requirement (or a severe \
seniority mismatch) that tailoring cannot fix — set no_fit_reason. "partial" = \
addressable gaps remain. "strong" = no blocking gaps.

IMPORTANT: seniority is a SOFT signal — many JDs title roles "Senior"/"Principal" \
while scoping them at director level. Do NOT raise a blocking seniority gap unless \
the mismatch is severe and explicit. Mark gaps the candidate's content already \
covers as addressable. Call submit_fit_assessment exactly once."""


def _build_prompt(jd, rubric, diagnostics, recommended) -> str:
    mix_lines = []
    for sid, r in sorted(recommended.items(), key=lambda kv: kv[0]):
        mix_lines.append(f"  - {sid}: from {r.source_cv} (section coverage {r.keyword_coverage})")
    return (
        f"ROLE: {jd.role_title}  (inferred seniority: {jd.seniority_level})\n"
        f"COMPANY CONTEXT: {jd.company_context}\n\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"JD NICE-TO-HAVES:\n" + "\n".join(f"  - {r}" for r in jd.nice_to_haves) + "\n\n"
        f"RUBRIC required keywords ({len(rubric.required_keywords)}): {rubric.required_keywords}\n"
        f"COVERED by the candidate's best composed sections: {diagnostics['matched_keywords']}\n"
        f"NOT COVERED: {diagnostics['missing_keywords']}\n"
        f"Composed-CV keyword coverage: {diagnostics['composed_coverage']}\n\n"
        f"RECOMMENDED SECTION MIX (best source per section):\n" + "\n".join(mix_lines) + "\n\n"
        f"Assess fit. Type each genuine gap, judge transferable strengths, and pick the outcome. "
        f"Missing keywords that are domain credentials the candidate lacks are gaps; missing keywords "
        f"the candidate clearly demonstrates in other words are addressable, not blocking."
    )


def _extract_tool_input(resp) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_fit_assessment":
            return block.input
    return None


def _validate_fit(data: dict) -> list[str]:
    problems = []
    if data.get("outcome") not in OUTCOMES:
        problems.append(f"outcome {data.get('outcome')!r} not in {sorted(OUTCOMES)}")
    if not isinstance(data.get("skills_transferable"), list):
        problems.append("skills_transferable must be a list")
    for i, g in enumerate(data.get("gaps", [])):
        if g.get("gap_type") not in GAP_TYPES:
            problems.append(f"gap[{i}].gap_type {g.get('gap_type')!r} invalid")
        if g.get("severity") not in SEVERITIES:
            problems.append(f"gap[{i}].severity {g.get('severity')!r} invalid")
    if data.get("outcome") == "no_fit" and not data.get("no_fit_reason"):
        problems.append("no_fit outcome requires no_fit_reason")
    return problems


def assess_fit(jd, rubric, *, model: str, config: dict, client=None, sections: list[dict] | None = None):
    """Produce a validated FitAssessment. Returns (FitAssessment, usage)."""
    sections = sections if sections is not None else all_sections(config)
    recommended, diagnostics = build_composition(sections, rubric, config["static_sections"])
    prompt = _build_prompt(jd, rubric, diagnostics, recommended)

    last_problems, data, usage = [], None, None
    for _ in range(2):
        resp = claude_complete(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_FIT_TOOL],
            tool_choice={"type": "tool", "name": "submit_fit_assessment"},
            max_tokens=1500,
            client=client,
        )
        usage = getattr(resp, "usage", None)
        data = _extract_tool_input(resp)
        if data is None:
            last_problems = ["model did not call submit_fit_assessment"]
            continue
        last_problems = _validate_fit(data)
        if not last_problems:
            break
    if data is None or last_problems:
        raise FitAssessmentError(f"fit assessment invalid after retry ({model}): " + "; ".join(last_problems))

    gaps = [
        FitGap(
            requirement=g["requirement"], gap_type=g["gap_type"],
            addressable=bool(g["addressable"]), severity=g["severity"], reason=g["reason"],
        )
        for g in data["gaps"]
    ]
    outcome = data["outcome"]
    fit = FitAssessment(
        outcome=outcome,
        overall_fit_score=diagnostics["composed_coverage"],   # deterministic (spec §4)
        skills_transferable=[str(s) for s in data["skills_transferable"]],
        gaps=gaps,
        recommended_sections=(None if outcome == "no_fit" else recommended),
        no_fit_reason=data.get("no_fit_reason"),
    )
    return fit, usage


# --------------------------------------------------------------------------- #
# HITL display (preview-before-apply, D-17/D-18)                              #
# --------------------------------------------------------------------------- #

def render_fit_hitl(fit: FitAssessment, jd) -> str:
    """Terminal HITL display for the fit assessment (readable, preview only)."""
    pct = f"{fit.overall_fit_score * 100:.0f}%"
    lines = [
        "─── Fit Assessment ──────────────────────────────────────────",
        f"  Role:    {jd.role_title}",
        f"  Outcome: {fit.outcome.upper().replace('_', ' ')}  (coverage: {pct})",
        "",
    ]
    if fit.outcome == "no_fit":
        lines += [f"  ⛔ {fit.no_fit_reason}", "", "  Options: [s]top  [o]verride and proceed anyway"]
        return "\n".join(lines)

    lines.append("  Recommended section mix:")
    for sid, r in sorted(fit.recommended_sections.items()):
        label = sid if len(sid) <= 44 else sid[:41] + "…"
        cov = "static" if r.reason.startswith("static") else f"{r.keyword_coverage:.0%}"
        lines.append(f"    {label:46} → {r.source_cv:18} {cov:>6}")
    if fit.skills_transferable:
        lines += ["", "  Transferable: " + ", ".join(fit.skills_transferable)]
    if fit.gaps:
        lines.append("  Gaps:")
        for g in fit.gaps:
            mark = "⛔" if g.severity == "blocking" else "⚠"
            addr = "addressable" if g.addressable else "not addressable"
            lines.append(f"    {mark} {g.requirement}  [{g.gap_type} / {g.severity} / {addr}]")
    lines += ["", "  Options: [p]roceed  [a]djust section mix  [s]top"]
    return "\n".join(lines)
