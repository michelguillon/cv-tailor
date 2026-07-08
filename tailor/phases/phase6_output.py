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

Experience role/date lines (F-29): the company heading is the section `title`
(F-23); the role line lives in `manifest[sid]["role_line"]`, split out at Phase 2
so the drafter can't drop it, and re-attached here (bold) between the heading and
the body. This is what keeps two role-groups at one employer (D-21) distinct.
"""

from __future__ import annotations

import difflib
import html
import json
import re
from pathlib import Path

import jinja2

from tailor.audit import read_entries
from tailor.helpers import strip_tool_artifacts
from tailor.models import (CritiqueItem, FitAssessment, IterationScore, JDAnalysis,
                           ScoringRubric)
from tailor.run_context import RunContext

__all__ = ["assemble_markdown", "generate_output", "summary_card",
           "build_report_context", "render_report", "reconstruct_report_inputs",
           "regenerate_html", "section_change", "section_change_from_disk",
           "load_final_manifest"]

_BOLD = re.compile(r"\*\*(.+?)\*\*")


# --------------------------------------------------------------------------- #
# Summary card (D-34) — the at-a-glance "should I submit this?" header         #
#                                                                              #
# Sourced from signals the pipeline already produces (F-43): grounded coverage #
# is the final iteration's source-grounded keyword_coverage (F-38); unsupported#
# claims is the verifier's flag count (F-35). No new LLM pass. This is the     #
# single source of truth for the card — api/archive.py reuses it.             #
# --------------------------------------------------------------------------- #

def summary_card(outcome: str, fit_score: float | None, grounded_coverage: float | None,
                 unsupported: int) -> dict:
    """Derive the sticky summary card fields. `fit_score`/`grounded_coverage` are
    fractions (0–1) or None; `unsupported` is the verifier flag count."""
    fit_pct = None if fit_score is None else round(fit_score * 100)
    grounded_pct = None if grounded_coverage is None else round(grounded_coverage * 100)
    if fit_pct is None:
        band = "low"
    elif fit_pct >= 75:
        band = "strong"
    elif fit_pct >= 40:
        band = "partial"
    else:
        band = "low"
    band_label = {"strong": "Strong", "partial": "Partial", "low": "No Fit / Review"}[band]
    if outcome == "no_fit":
        status = "Do Not Submit"
    elif unsupported > 0 or fit_pct is None or fit_pct < 75:
        status = "Review Required"
    else:
        status = "Submit-ready"
    return {"fit_label": band_label, "fit_pct": fit_pct, "fit_band": band,
            "grounded_pct": grounded_pct, "unsupported": unsupported, "status": status}


def _inline(text: str) -> str:
    """Escape, then render markdown **bold** as <strong> — used for the experience
    role/date lines re-attached at assembly (F-29) and any bold in section text."""
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text))

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
            continue
        heading = f"## {m.get('title') or sid}"
        # Re-attach the experience role/date line(s) the drafter never saw (F-29) —
        # bold, one per line, between the company heading and the bulleted body.
        # This is what makes two role-groups at one employer distinct (D-21/F-23).
        role_line = m.get("role_line")
        if role_line:
            role_md = "\n".join(f"**{ln.strip()}**" for ln in role_line.splitlines() if ln.strip())
            blocks.append(f"{heading}\n\n{role_md}\n\n{body}")
        else:
            blocks.append(f"{heading}\n\n{body}")
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
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(line.lstrip()[2:])}</li>")
        elif not line:
            if in_ul:
                out.append("</ul>"); in_ul = False
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<p>{_inline(line)}</p>")
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


def section_change(ctx, manifest: dict, sid: str) -> dict | None:
    """The Changes-tab entry for one section: version labels + the v0→final word diff
    (or rendered latest when there's a single version). None if the section is absent or
    has no version files. Static sections are copied verbatim (D-13). Shared by the report
    and the `/sections/{sid}/diff` endpoint (SPEC_SQLITE_MIGRATION §5.1)."""
    m = manifest.get(sid)
    if m is None:
        return None
    disp = m.get("label") or m.get("title") or sid
    if m["static"]:
        return {"sid": sid, "title": disp, "static": True,
                "versions": ["static"], "diff_html": "(copied verbatim)"}
    versions = _section_versions(ctx, manifest, sid)
    if not versions:
        return None
    first, last = versions[0][1], versions[-1][1]
    return {
        "sid": sid, "title": disp, "static": False,
        "versions": [lbl for lbl, _ in versions],
        "diff_html": _word_diff_html(first, last) if len(versions) > 1 else _md_to_html(last),
    }


def _build_changes(ctx, manifest, config) -> list[dict]:
    return [c for sid in _ordered_ids(manifest, config)
            if (c := section_change(ctx, manifest, sid)) is not None]


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
        # run_log.jsonl also holds non-reasoning records (the run_complete cost footer has
        # no phase/event) — skip them so the trace doesn't render an empty "?" group (F-40).
        if not e.get("phase") or not e.get("event"):
            continue
        grouped.setdefault(e["phase"], []).append(e)
    return [{"phase": ph, "entries": evs} for ph, evs in grouped.items()]


def _job_radar_source(output_dir: Path) -> dict | None:
    """The Job Radar provenance, if this run came from a Job Radar handoff (Integration §5.2).

    Read defensively from the `run_meta.json` sidecar — an API-layer file (absent for CLI runs),
    so this is read straight off disk with no dependency on `api/` (which itself imports `tailor`)."""
    path = output_dir / "run_meta.json"
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    src = meta.get("job_radar_source") if isinstance(meta, dict) else None
    return src if isinstance(src, dict) else None


def _build_grounding(flags) -> dict:
    """Provenance for the report's Grounding tab (F-35): the verifier's unsupported-claim
    flags, flattened. `flags` is {section_id: [CritiqueItem, ...]} or None."""
    flags = flags or {}
    claims = [{"section": sid, "issue": it.issue, "suggestion": it.suggestion}
              for sid, fl in flags.items() for it in fl]
    return {"total": len(claims), "sections": len(flags), "claims": claims}


def build_report_context(ctx, manifest, jd, fit, final_rubric, iterations, *,
                         config, verification_flags=None, jd_raw: str = "",
                         cv_md: str | None = None) -> dict:
    """Assemble the Jinja context for the report template (the 7 tabs + summary card).

    Pure (no file writes): the single place the report's shape is defined, shared by the
    run-time `generate_output` and the on-demand `regenerate_html` (so the two can't drift).
    `cv_md` is assembled if not supplied."""
    cv_md = assemble_markdown(ctx, manifest, config) if cv_md is None else cv_md
    grounding = _build_grounding(verification_flags)
    grounded_coverage = iterations[-1].keyword_coverage if iterations else None
    return {
        "role_title": jd.role_title,
        "outcome": fit.outcome,
        "fit_score": round(fit.overall_fit_score, 3),
        # Sticky summary card (D-34) — sourced from existing signals (F-43): grounded
        # coverage = final iteration's source-grounded keyword_coverage (F-38);
        # unsupported = verifier flag count (F-35). No new LLM pass.
        "summary_card": summary_card(fit.outcome, fit.overall_fit_score,
                                     grounded_coverage, grounding["total"]),
        # Overall CV quality = the final iteration's aggregate critique_score (0–10, the
        # "quality" column of the Scores tab). Shown in the header alongside coverage so the
        # at-a-glance card answers "how good is the writing", not just "how well does it match".
        "quality_score": (None if not iterations or iterations[-1].critique_score is None
                          else round(iterations[-1].critique_score, 1)),
        # Job Radar provenance (Integration §5.2), if this run came from a handoff — header badge.
        "job_radar_source": _job_radar_source(ctx.output_dir),
        "jd_raw": jd_raw,                                  # raw JD for the JD tab (D-37)
        # Role-fit summary (F-39): the CVCM value-alignment narrative + transferable
        # strengths + gaps, so "why am I a fit" is visible after any run (incl. --yes/auto,
        # which never pauses at the Phase-1 checkpoint). value_alignment_notes is None
        # without a CVCM (D-33) — the template falls back to the no-fit reason.
        "value_alignment_notes": getattr(fit, "value_alignment_notes", None),
        "no_fit_reason": fit.no_fit_reason,
        "skills_transferable": list(fit.skills_transferable),
        "gaps": fit.gaps,
        "converged": iterations[-1].keyword_coverage if iterations else None,
        "cv_html": _md_to_html(cv_md),
        "changes": _build_changes(ctx, manifest, config),
        "scores": _build_scores(manifest, iterations, config),
        "reasoning": _build_reasoning(ctx),
        "grounding": grounding,
        "rubric": final_rubric,
        "run_id": ctx.run_id,
    }


def render_report(context: dict, *, template_dir: str | Path = TEMPLATE_DIR) -> str:
    """Render the report template to an HTML string (no file write)."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    return env.get_template(TEMPLATE_NAME).render(**context)


def generate_output(ctx, manifest, jd, fit, final_rubric, iterations, *,
                    config, template_dir: str | Path = TEMPLATE_DIR,
                    source_docx=None, verification_flags=None, jd_raw: str = "") -> dict:
    """Write cv_final.md + cv_final.html (+ cv_final.docx when `source_docx` is given,
    the --docx stretch). `verification_flags` ({sid: [CritiqueItem]}) feeds the report's
    Grounding tab (F-35); `jd_raw` is the raw JD for the JD tab (D-37). Returns
    {'md', 'html'[, 'docx']} paths."""
    cv_md = assemble_markdown(ctx, manifest, config)
    md_path = ctx.output_dir / "cv_final.md"
    md_path.write_text(cv_md, encoding="utf-8")

    context = build_report_context(ctx, manifest, jd, fit, final_rubric, iterations,
                                   config=config, verification_flags=verification_flags,
                                   jd_raw=jd_raw, cv_md=cv_md)
    html_out = render_report(context, template_dir=template_dir)
    html_path = ctx.output_dir / "cv_final.html"
    html_path.write_text(html_out, encoding="utf-8")

    ctx.audit.log_event("phase6_output", "output_written",
                        f"cv_final.md ({len(cv_md.split())} words) + cv_final.html")

    out = {"md": str(md_path), "html": str(html_path)}
    if source_docx is not None:                      # --docx stretch (clean CV only)
        from tailor.phases import phase6_docx
        docx_path = phase6_docx.write_cv_docx(cv_md, source_docx, ctx.output_dir / "cv_final.docx")
        ctx.audit.log_event("phase6_output", "docx_written",
                            f"cv_final.docx (formatting from {Path(source_docx).name})")
        out["docx"] = str(docx_path)
    return out


# --------------------------------------------------------------------------- #
# On-demand report regeneration from disk (SPEC_SQLITE_MIGRATION §4.3)         #
#                                                                             #
# The report is fully reconstructable from a run's checkpoints — this is what #
# lets the static cv_final.html become an on-demand artifact (Phase 3 retires #
# the run-time write entirely). Promoted from the tmp/sweep/regen_report.py   #
# helper (F-40) into a supported path.                                        #
# --------------------------------------------------------------------------- #

def load_final_manifest(run_dir: str | Path) -> dict:
    """The run's final section manifest: prefer `final_manifest.json` (post-refinement
    state, F-40); for an older run reconstruct the latest version per non-static section
    from the on-disk section files."""
    run_dir = Path(run_dir)
    if (run_dir / "final_manifest.json").exists():
        return json.loads((run_dir / "final_manifest.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "phase2_draft_manifest.json").read_text(encoding="utf-8"))
    for sid, m in manifest.items():
        if not m["static"]:
            vs = [int(p.stem.rsplit("_v", 1)[1])
                  for p in (run_dir / "sections").glob(f"{sid}_v*.md")]
            if vs:
                m["version"] = max(vs)
    return manifest


def section_change_from_disk(run_dir: str | Path, sid: str) -> dict | None:
    """The Changes-tab entry for one section, reconstructed from a run's checkpoints —
    behind `GET /api/runs/{id}/sections/{sid}/diff` (§5.1). None if the section is unknown
    or has no version files."""
    run_dir = Path(run_dir)
    ctx = RunContext(run_id=run_dir.name, output_dir=run_dir)
    return section_change(ctx, load_final_manifest(run_dir), sid)


def _reconstruct_flags(run_dir: Path) -> dict:
    """Rebuild the verifier's grounding flags from the run_log `unsupported_claim` events
    (reason = 'sid: issue'). {sid: [CritiqueItem, ...]}."""
    flags: dict[str, list] = {}
    for e in read_entries(run_dir / "run_log.jsonl"):
        if e.get("phase") == "verification" and e.get("event") == "unsupported_claim":
            sid, _, issue = (e.get("reasoning", "")).partition(": ")
            flags.setdefault(sid, []).append(CritiqueItem(
                section=sid, severity="major", issue=issue or e.get("reasoning", ""),
                suggestion="Verify against your source CV before sending.", source_writer="verifier"))
    return flags


def reconstruct_report_inputs(run_dir: str | Path, config: dict) -> dict:
    """Rebuild generate_output's inputs from a run's on-disk checkpoints (§4.3). Returns
    a dict of {ctx, manifest, jd, fit, rubric, iterations, flags, jd_raw}. Faithful:
    prefers final_manifest.json (else reconstructs versions from the section files),
    rebuilds grounding flags from the run_log, cleans stored fit text (F-40)."""
    run_dir = Path(run_dir)

    def jload(name):
        return json.loads((run_dir / name).read_text(encoding="utf-8"))

    ctx = RunContext(run_id=run_dir.name, output_dir=run_dir)
    jd = JDAnalysis.from_dict(jload("phase0_jd_analysis.json"))
    fit = FitAssessment.from_dict(jload("phase1_fit_assessment.json"))
    fit.value_alignment_notes = strip_tool_artifacts(fit.value_alignment_notes)   # F-40
    fit.no_fit_reason = strip_tool_artifacts(fit.no_fit_reason)
    rubric = ScoringRubric.from_dict(jload("phase0_rubric.json"))
    manifest = load_final_manifest(run_dir)

    iterations, i = [], 1
    while (run_dir / f"iteration_{i}.json").exists():
        iterations.append(IterationScore.from_dict(jload(f"iteration_{i}.json")))
        i += 1

    jd_raw_path = run_dir / "jd_raw.txt"
    jd_raw = jd_raw_path.read_text(encoding="utf-8") if jd_raw_path.exists() else ""

    return {"ctx": ctx, "manifest": manifest, "jd": jd, "fit": fit, "rubric": rubric,
            "iterations": iterations, "flags": _reconstruct_flags(run_dir), "jd_raw": jd_raw}


def regenerate_html(run_dir: str | Path, *, config: dict,
                    template_dir: str | Path = TEMPLATE_DIR) -> str:
    """Regenerate the report HTML for an existing run from its checkpoints, as a string
    (no file write) — the on-demand path behind `GET /api/runs/{id}/html` (§4.3)."""
    p = reconstruct_report_inputs(run_dir, config)
    context = build_report_context(p["ctx"], p["manifest"], p["jd"], p["fit"], p["rubric"],
                                   p["iterations"], config=config,
                                   verification_flags=p["flags"] or None, jd_raw=p["jd_raw"])
    return render_report(context, template_dir=template_dir)
