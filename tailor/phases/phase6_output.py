"""Phase 6 — Output generation. SPEC §5 Phase 6, §9. Step 7.

Input:  the manifest (section_type/position/title/version/static), section files,
        JDAnalysis, FitAssessment, final ScoringRubric, list[IterationScore],
        run_log.jsonl. RunContext.
Output: outputs/<run_id>/cv_final.md (clean, submittable) + cv_final.html (the
        4-tab review surface: CV / Changes / Scores / Reasoning).

Assembly is checkpoint-driven (D-07 #3): sections are read from disk and ordered
by (config `cv_sections` type order, then `position`) — both carried in the
manifest, so Phase 6 never re-queries the corpus. The "base CV metadata position"
of the original spec doesn't apply under section-mixing (sources differ per
section), so we order by section_type first and use the source `position` only as
a within-type tiebreak (mainly the experience block); cross-CV positions are
imperfect but deterministic (F-23).
"""

from __future__ import annotations

import difflib
import html
from pathlib import Path

import jinja2

from tailor.audit import read_entries

__all__ = ["assemble_markdown", "generate_output"]

TEMPLATE_DIR = Path("templates")
TEMPLATE_NAME = "output.html"


def _ordered_ids(manifest: dict, config: dict) -> list[str]:
    type_order = {t: i for i, t in enumerate(config.get("cv_sections", []))}
    return sorted(
        manifest,
        key=lambda sid: (type_order.get(manifest[sid]["section_type"], 99),
                         manifest[sid].get("position", 0), sid),
    )


def _latest_text(ctx, manifest, sid: str) -> str:
    m = manifest[sid]
    if m["static"]:
        return ctx.read_section(sid, static=True)
    return ctx.read_section(sid, version=m["version"])


def assemble_markdown(ctx, manifest: dict, config: dict) -> str:
    """Assemble the clean CV markdown (the submittable artefact, §9 cv_final.md)."""
    blocks = []
    for sid in _ordered_ids(manifest, config):
        m = manifest[sid]
        body = _latest_text(ctx, manifest, sid).strip()
        if m["section_type"] == "header":
            blocks.append(body)               # name + contact: no heading
        else:
            blocks.append(f"## {m.get('title') or sid}\n\n{body}")
    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- #
# HTML rendering helpers                                                       #
# --------------------------------------------------------------------------- #

def _md_to_html(md: str) -> str:
    """Tiny markdown → HTML (headings, bullet lists, paragraphs). No deps."""
    out, in_ul = [], False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{html.escape(line.lstrip()[2:])}</li>")
        elif not line:
            if in_ul:
                out.append("</ul>"); in_ul = False
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<p>{html.escape(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _word_diff_html(before: str, after: str) -> str:
    """Word-level diff: additions green, removals red (§9 Changes tab)."""
    a, b = before.split(), after.split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    spans = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            spans.append(html.escape(" ".join(a[i1:i2])))
        else:
            if i1 != i2:
                spans.append(f'<del>{html.escape(" ".join(a[i1:i2]))}</del>')
            if j1 != j2:
                spans.append(f'<ins>{html.escape(" ".join(b[j1:j2]))}</ins>')
    return " ".join(s for s in spans if s)


def _section_versions(ctx, manifest, sid: str) -> list[tuple[str, str]]:
    """(label, text) for v0..v(latest) of a non-static section that exist on disk."""
    out = []
    latest = manifest[sid]["version"] or 0
    for v in range(0, latest + 1):
        p = ctx.section_path(sid, version=v)
        if p.exists():
            out.append((f"v{v}", p.read_text(encoding="utf-8").strip()))
    return out


def _build_changes(ctx, manifest, config) -> list[dict]:
    changes = []
    for sid in _ordered_ids(manifest, config):
        m = manifest[sid]
        disp = m.get("label") or m.get("title") or sid
        if m["static"]:
            changes.append({"sid": sid, "title": disp, "static": True,
                            "versions": ["static"], "diff_html": "(copied verbatim)"})
            continue
        versions = _section_versions(ctx, manifest, sid)
        if not versions:
            continue
        first, last = versions[0][1], versions[-1][1]
        changes.append({
            "sid": sid, "title": disp, "static": False,
            "versions": [lbl for lbl, _ in versions],
            "diff_html": _word_diff_html(first, last) if len(versions) > 1 else _md_to_html(last),
        })
    return changes


def _build_scores(manifest, iterations, config) -> dict:
    order = [sid for sid in _ordered_ids(manifest, config) if not manifest[sid]["static"]]
    rows = []
    for sid in order:
        cells = []
        for it in iterations:
            s = it.section_scores.get(sid)
            if s is None:
                cells.append(None)
            else:
                cells.append({
                    "coverage": round(s.keyword_coverage, 3),
                    "claude": s.claude_quality, "gpt": s.gpt_quality,
                    "selected": s.selected_writer, "converged": s.converged,
                })
        rows.append({"sid": sid, "title": manifest[sid].get("label") or manifest[sid].get("title") or sid,
                     "cells": cells})
    aggregate = [{
        "iteration": it.iteration,
        "coverage": round(it.keyword_coverage, 3),
        "quality": (None if it.critique_score is None else round(it.critique_score, 2)),
        "keyword_delta": round(it.keyword_delta, 3), "quality_delta": round(it.quality_delta, 2),
        "converged": it.sections_converged, "active": it.sections_active,
    } for it in iterations]
    return {"iterations": [it.iteration for it in iterations], "rows": rows, "aggregate": aggregate}


def _build_reasoning(ctx) -> list[dict]:
    """Audit entries grouped by phase, in order (the collapsible Reasoning trace)."""
    entries = read_entries(ctx.output_dir / "run_log.jsonl")
    grouped: dict[str, list] = {}
    for e in entries:
        grouped.setdefault(e.get("phase", "?"), []).append(e)
    return [{"phase": ph, "entries": evs} for ph, evs in grouped.items()]


def generate_output(ctx, manifest, jd, fit, final_rubric, iterations, *,
                    config, template_dir: str | Path = TEMPLATE_DIR) -> dict:
    """Write cv_final.md + cv_final.html. Returns {'md': path, 'html': path}."""
    cv_md = assemble_markdown(ctx, manifest, config)
    md_path = ctx.output_dir / "cv_final.md"
    md_path.write_text(cv_md, encoding="utf-8")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    template = env.get_template(TEMPLATE_NAME)
    context = {
        "role_title": jd.role_title,
        "outcome": fit.outcome,
        "fit_score": round(fit.overall_fit_score, 3),
        "converged": iterations[-1].keyword_coverage if iterations else None,
        "cv_html": _md_to_html(cv_md),
        "changes": _build_changes(ctx, manifest, config),
        "scores": _build_scores(manifest, iterations, config),
        "reasoning": _build_reasoning(ctx),
        "rubric": final_rubric,
        "run_id": ctx.run_id,
    }
    html_out = template.render(**context)
    html_path = ctx.output_dir / "cv_final.html"
    html_path.write_text(html_out, encoding="utf-8")

    ctx.audit.log_event("phase6_output", "output_written",
                        f"cv_final.md ({len(cv_md.split())} words) + cv_final.html")
    return {"md": str(md_path), "html": str(html_path)}
