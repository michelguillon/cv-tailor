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

from tailor.candidate import CVCM_FRAMING_NOTE
from tailor.helpers import claude_complete
from tailor.tools.writer_common import TRUTHFULNESS_RULES

__all__ = ["draft_sections", "DraftError"]


class DraftError(RuntimeError):
    pass


# Phase 2 originates the first draft, so it carries the SAME anti-fabrication rules as
# the Phase-3 writers (F-34/F-35) — it's where a fabricated headline/sector first crept
# in. Shared from writer_common so the two never drift.
_SYSTEM = f"""\
You tailor ONE CV section to a specific job, truthfully. Rewrite the SOURCE \
section so it emphasises what matters for THIS role and surfaces the listed \
keywords ONLY where they are genuinely supported by the source content.

{TRUTHFULNESS_RULES}
- Aim for about {{target}} words — stay close to the SOURCE length. Do NOT pad a \
short section to fill space; a terse role stays terse.
- Output ONLY the section text (markdown), no heading, no preamble, no commentary."""


def _split_role_line(document: str) -> tuple[str, str]:
    """Split an experience section into its leading role/date line(s) and the
    bulleted body. The role line (e.g. "Senior Product Manager (Apr 2022 – Mar
    2024)") is a structural FACT: the drafter, told "no heading", drops or rewrites
    it inconsistently (F-29) — Microsoft lost it while Utiq kept it. So we keep it
    out of the draftable text entirely and re-attach it verbatim at assembly
    (Phase 6). Promotion stacks (D-21) have several leading role lines before the
    first bullet — all are captured. Returns (role_line, body); ('', document) when
    there is no leading non-bullet line followed by bullets."""
    lines = document.splitlines()
    i = 0
    while i < len(lines) and not lines[i].lstrip().startswith(("- ", "* ")):
        i += 1
    if i == 0 or i == len(lines):       # starts with a bullet, or has no bullets at all
        return "", document.strip()
    role_line = "\n".join(ln.rstrip() for ln in lines[:i] if ln.strip())
    body = "\n".join(lines[i:]).strip()
    return role_line, body


def _source_lookup(sections: list[dict]) -> dict[tuple[str, str], dict]:
    """Map (short_cv, section_id) → section dict, matching SectionRecommendation."""
    out = {}
    for s in sections:
        short = s["filename"].replace("CV_Michel_Guillon_2026_", "").replace(".docx", "")
        out[(short, s["section_id"])] = s
    return out


def _draft_one(jd, rubric, section_type, target_words, source_text, *, model, client, cvcm=None) -> str:
    missing = rubric.required_keywords  # surface where truthful; loop refines later
    cvcm_block = (
        f"CANDIDATE VALUE MODEL (how the candidate creates value — use for framing/emphasis "
        f"only). {CVCM_FRAMING_NOTE}\n{cvcm}\n\n" if cvcm else ""
    )
    user = (
        cvcm_block +
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


def draft_sections(fit, jd, rubric, sections, budgets, ctx, *, model, client=None, cvcm=None) -> dict:
    """Draft every recommended section to disk. Returns a manifest dict.

    manifest[section_id] = {static, version, word_count, source_cv, path,
                            section_type, position, title, label[, role_line]}
    The manifest is the input contract for Phase 3 (section_type → no corpus
    re-read) and Phase 6 (position + title/label → checkpoint-driven assembly).
    `title` is the CV heading; `label` disambiguates status/table displays.
    `role_line` (experience only) is the section's verbatim role/date line, held
    out of the drafted body and re-attached at assembly so it can't be dropped (F-29).
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
        # Carried for Phase 6 assembly (order) + headings, so output is checkpoint-driven.
        position = src.get("position", 0)
        # `title` = the CV heading (company for experience — the role is already in the
        # body's bold line, so the CV body isn't ambiguous). `label` disambiguates the
        # status/Changes/Scores displays, where two role-groups at one employer would
        # otherwise collapse to one identical line (e.g. two "AppNexus / Xandr" — the
        # `/` is the acquisition, legitimate; the two ROLES are what must be told apart).
        company = src.get("company")
        role = src.get("title", "")
        title = company or role or section_id
        label = f"{company} — {role}" if (company and role and role != company) else title

        if src["static"]:
            path = ctx.write_section(section_id, src["document"], static=True)
            audit.log_event("phase2_draft", "static_copied",
                            f"{section_id} copied verbatim from {rec.source_cv}")
            manifest[section_id] = {
                "static": True, "version": None, "word_count": len(src["document"].split()),
                "source_cv": rec.source_cv, "path": str(path), "section_type": section_type,
                "position": position, "title": title, "label": label,
            }
            continue

        # Experience sections lead with a role/date line; keep it out of the
        # draftable text so the LLM can't drop it (F-29) and re-attach it at
        # assembly (Phase 6). Other section types have no role line.
        role_line, source_doc = ("", src["document"])
        if section_type == "experience":
            role_line, source_doc = _split_role_line(src["document"])

        # Anchor the target to the SOURCE length, clamped to the budget (F-13).
        # The type-median target over-inflates short role sections (a 23-word
        # early role would be padded toward 108 — fabrication risk). Tailoring
        # reweights wording; it shouldn't massively change length.
        source_wc = len(source_doc.split())
        budget = budgets.get(section_type)
        if budget:
            target = min(max(source_wc, budget.min_words), budget.max_words)
        else:
            target = source_wc or 120
        # Persist the raw corpus body as the ground truth every later phase verifies
        # against (F-35) — the drafter tailors from this; the orchestrator and the
        # verification gate check that nothing was added beyond it.
        ctx.write_section(section_id, source_doc, source=True)
        text = _draft_one(jd, rubric, section_type, target, source_doc,
                          model=model, client=client, cvcm=cvcm)
        path = ctx.write_section(section_id, text, version=0)
        wc = len(text.split())
        audit.log_event("phase2_draft", "section_drafted",
                        f"{section_id} drafted from {rec.source_cv} (~{target}w target, {wc}w actual)")
        entry = {
            "static": False, "version": 0, "word_count": wc,
            "source_cv": rec.source_cv, "path": str(path), "section_type": section_type,
            "position": position, "title": title, "label": label,
        }
        if section_type == "experience":
            entry["role_line"] = role_line   # structural; re-attached at assembly (F-29)
        manifest[section_id] = entry

    ctx.write_checkpoint("phase2_draft_manifest", manifest)
    return manifest
