"""Phase 2 — Initial draft (Claude). SPEC §5 Phase 2.

Input:  FitAssessment.recommended_sections, JDAnalysis, ScoringRubric, budgets,
        the corpus sections (for source text).
Output: outputs/<run_id>/sections/<id>_v0.md per non-static section;
        <id>_static.md per static section. A draft manifest.
Model:  Claude (Haiku/dev, Sonnet/full).

Each non-static section is drafted INDEPENDENTLY from its recommended source
(D-12: section is the unit of work), tailored toward the JD/rubric within the
section's target_words (D-14). Static sections are copied verbatim (D-13).

Load-bearing constraint: the drafter tailors emphasis, ordering, and wording —
it must NOT invent employers, titles, dates, metrics, or experience. A CV tool
that fabricates is worse than useless. This is stated explicitly in the prompt.
"""

from __future__ import annotations

from tailor.helpers import claude_complete

__all__ = ["draft_sections", "DraftError"]


class DraftError(RuntimeError):
    pass


_SYSTEM = """\
You tailor ONE CV section to a specific job, truthfully. Rewrite the SOURCE \
section so it emphasises what matters for THIS role and surfaces the listed \
keywords ONLY where they are genuinely supported by the source content.

HARD RULES:
- Do NOT invent or alter employers, job titles, dates, companies, metrics, or \
facts. Change emphasis, ordering, and wording only.
- Keep every factual claim traceable to the source. If a keyword isn't supported, \
leave it out — do not fabricate experience to match the JD.
- Preserve the source's structure: if it uses bullet points, return bullet points.
- Aim for about {target} words — stay close to the SOURCE length. Do NOT pad a \
short section to fill space; a terse role stays terse.
- Output ONLY the section text (markdown), no heading, no preamble, no commentary."""


def _source_lookup(sections: list[dict]) -> dict[tuple[str, str], dict]:
    """Map (short_cv, section_id) → section dict, matching SectionRecommendation."""
    out = {}
    for s in sections:
        short = s["filename"].replace("CV_Michel_Guillon_2026_", "").replace(".docx", "")
        out[(short, s["section_id"])] = s
    return out


def _draft_one(jd, rubric, section_type, target_words, source_text, *, model, client) -> str:
    missing = rubric.required_keywords  # surface where truthful; loop refines later
    user = (
        f"ROLE: {jd.role_title}\n"
        f"SECTION TYPE: {section_type}\n"
        f"TARGET WORDS: ~{target_words}\n\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"KEYWORDS TO SURFACE WHERE TRUTHFUL: {missing}\n\n"
        f"SOURCE SECTION:\n{source_text}"
    )
    resp = claude_complete(
        model=model,
        system=_SYSTEM.format(target=target_words),
        messages=[{"role": "user", "content": user}],
        max_tokens=max(256, target_words * 6),
        temperature=0.0,
        client=client,
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise DraftError(f"empty draft for a {section_type} section")
    return text


def draft_sections(fit, jd, rubric, sections, budgets, ctx, *, model, client=None) -> dict:
    """Draft every recommended section to disk. Returns a manifest dict.

    manifest[section_id] = {static, version, word_count, source_cv, path, section_type}
    The manifest is Phase 3's sole input contract: it carries section_type so the
    refinement loop is fully checkpoint-driven and never re-reads the corpus.
    """
    if fit.recommended_sections is None:
        raise DraftError("no recommended_sections (no_fit outcome) — nothing to draft")

    lookup = _source_lookup(sections)
    audit = ctx.audit
    manifest: dict[str, dict] = {}

    for section_id, rec in fit.recommended_sections.items():
        src = lookup.get((rec.source_cv, section_id))
        if src is None:
            raise DraftError(f"source section not found: {rec.source_cv}/{section_id}")
        section_type = src["section_type"]

        if src["static"]:
            path = ctx.write_section(section_id, src["document"], static=True)
            audit.log_event("phase2_draft", "static_copied",
                            f"{section_id} copied verbatim from {rec.source_cv}")
            manifest[section_id] = {
                "static": True, "version": None, "word_count": len(src["document"].split()),
                "source_cv": rec.source_cv, "path": str(path), "section_type": section_type,
            }
            continue

        # Anchor the target to the SOURCE length, clamped to the budget (F-13).
        # The type-median target over-inflates short role sections (a 23-word
        # early role would be padded toward 108 — fabrication risk). Tailoring
        # reweights wording; it shouldn't massively change length.
        source_wc = len(src["document"].split())
        budget = budgets.get(section_type)
        if budget:
            target = min(max(source_wc, budget.min_words), budget.max_words)
        else:
            target = source_wc or 120
        text = _draft_one(jd, rubric, section_type, target, src["document"], model=model, client=client)
        path = ctx.write_section(section_id, text, version=0)
        wc = len(text.split())
        audit.log_event("phase2_draft", "section_drafted",
                        f"{section_id} drafted from {rec.source_cv} (~{target}w target, {wc}w actual)")
        manifest[section_id] = {
            "static": False, "version": 0, "word_count": wc,
            "source_cv": rec.source_cv, "path": str(path), "section_type": section_type,
        }

    ctx.write_checkpoint("phase2_draft_manifest", manifest)
    return manifest
