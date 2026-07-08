"""tailor/run.py — pipeline assembly: Phase 0 → 6, HITL, cost footer. Step 8.

The deterministic scaffold around the agentic loop (D-01). `run_pipeline` sequences
the phases, pausing at the three HITL checkpoints (fit / section review /
formatting). The checkpoints are delegated to a HITL handler so the same pipeline
serves the interactive CLI (`TerminalHITL`) and tests / non-interactive runs
(`AutoHITL`) — the phases only ever *render*; the input lives here (like Phase 1's
render_fit_hitl). Cost is captured centrally via cost.track() (D-08, estimates F-08).
"""

from __future__ import annotations

import logging
from pathlib import Path

from corpus.retrieval import all_sections
from tailor import cost, db, telemetry
from tailor.candidate import load_cvcm
from tailor.config import load_budgets, load_config, resolve_run_config
from tailor.phases import phase4_hitl, phase5_validation
from tailor.phases.phase0_jd_analysis import analyse_jd
from tailor.phases.phase1_fit_assessment import assess_fit, render_fit_hitl
from tailor.phases.phase2_initial_draft import draft_sections
from tailor.phases.phase3_refinement import refine
from tailor.phases.phase6_output import generate_output
from tailor.run_context import RunContext
from tailor.tools import verifier

__all__ = ["run_pipeline", "AutoHITL", "TerminalHITL", "PipelineStop"]


class PipelineStop(RuntimeError):
    """Raised to stop the pipeline early at a human's request (e.g. no_fit, stop)."""


# --------------------------------------------------------------------------- #
# HITL handlers — same pipeline, different front end                          #
# --------------------------------------------------------------------------- #

class AutoHITL:
    """Non-interactive: proceed through every checkpoint, accept all, no edits.
    Used by tests and `--yes`. Stops only on a no_fit it isn't told to override."""

    def __init__(self, *, override_no_fit: bool = False):
        self.override_no_fit = override_no_fit

    def fit(self, fit, jd) -> bool:
        return fit.outcome != "no_fit" or self.override_no_fit

    def review(self, ctx, result, jd, rubric, budgets, rc) -> None:
        return None                       # accept all, no revisions

    def formatting(self, corrections, length) -> bool:
        return True                       # apply formatting corrections


class TerminalHITL:
    """Interactive terminal checkpoints (CLI). Preview-before-apply throughout."""

    def __init__(self, *, input_fn=input, print_fn=print):
        self.input = input_fn
        self.print = print_fn

    def fit(self, fit, jd) -> bool:
        self.print(render_fit_hitl(fit, jd))
        ans = self.input("  > ").strip().lower()
        if fit.outcome == "no_fit":
            return ans in ("o", "override")
        return ans not in ("s", "stop")

    def review(self, ctx, result, jd, rubric, budgets, rc) -> None:
        while True:
            self.print(phase4_hitl.render_section_review(result, max_iterations=rc.max_iterations))
            ans = self.input("  > ").strip().lower()
            if ans in ("a", "d", ""):
                return
            if ans.startswith("b"):
                items = phase4_hitl.unresolved_list(result)
                try:
                    sid, it = items[int(ans[1:]) - 1]
                except (ValueError, IndexError):
                    self.print("  ? unknown item number"); continue
                v, _ = phase4_hitl.revise_section(sid, it.suggestion, result, jd, rubric, budgets,
                                                  ctx, model=rc.orchestrator_model)
                self.print(f"  → {sid} revised to v{v}")
            elif ans.startswith("e"):
                txt = self.input("  describe the change: ").strip()
                try:
                    dec = phase4_hitl.interpret_freetext(txt, result, model=rc.validation_model)
                except phase4_hitl.HITLError as exc:
                    self.print(f"  ? {exc}"); continue
                self.print(f"  interpreted → {dec['section_id']}: {dec['instruction']}")
                if self.input("  apply? [y/n] ").strip().lower().startswith("y"):
                    v, _ = phase4_hitl.revise_section(dec["section_id"], dec["instruction"], result,
                                                      jd, rubric, budgets, ctx, model=rc.orchestrator_model)
                    self.print(f"  → {dec['section_id']} revised to v{v}")
            else:
                self.print("  ? options: a / b<n> / d / e")

    def formatting(self, corrections, length) -> bool:
        self.print(phase5_validation.render_corrections(corrections, length))
        return self.input("  > ").strip().lower().startswith("y")


# --------------------------------------------------------------------------- #
# The pipeline                                                                 #
# --------------------------------------------------------------------------- #

def run_pipeline(jd_path, *, mode="demo", key=None, max_iterations=None,
                 output_dir="outputs", dry_run=False, hitl=None, run_id=None,
                 on_event=None, docx=False) -> dict:
    """Run Phase 0→6 (or 0→1 for --dry-run). Returns a summary dict.

    `on_event` (optional) is called with a progress dict at each phase boundary —
    the hook the Web UI's SSE stream consumes (SPEC §12.2). It is a no-op for the
    CLI (None), so the pipeline stays identical; never let an emit raise."""
    def emit(type_, **fields):
        if on_event is not None:
            on_event({"type": type_, **fields})

    config = load_config()
    budgets = load_budgets()
    rc = resolve_run_config(config, mode=mode, key=key, max_iterations=max_iterations)
    hitl = hitl or TerminalHITL()
    ctx = RunContext.create(run_id=run_id, base_dir=output_dir)
    jd_text = Path(jd_path).read_text(encoding="utf-8")
    # Persist the raw JD so the run dir is self-describing (F-40) and Phase 6 can render
    # the JD tab (D-37) — and a report can be regenerated from disk with the JD intact.
    (ctx.output_dir / "jd_raw.txt").write_text(jd_text, encoding="utf-8")
    cvcm = load_cvcm()        # optional candidate value model (§3.9/D-33); None if absent

    summary = {"run_id": ctx.run_id, "mode": rc.mode, "output_dir": str(ctx.output_dir)}

    with cost.track() as tracker:
        # Phase 0 — JD analysis (Mistral). This one provider call doesn't go through a
        # cost-noting helper (it uses the Mistral client directly), so note its usage
        # here. Phase 1+ go through claude_complete/gpt_complete, which note themselves.
        emit("phase_start", phase="phase0_jd_analysis", label="JD analysis")
        with telemetry.span("phase0_jd_analysis") as _sp0, \
                telemetry.generation("mistral_extraction", model=rc.jd_model, input=jd_text) as _gen0:
            jd, rubric, jd_usage = analyse_jd(jd_text, model=rc.jd_model)
            if jd_usage is not None:
                in_tok = getattr(jd_usage, "prompt_tokens", 0) or 0
                out_tok = getattr(jd_usage, "completion_tokens", 0) or 0
                cost.note(rc.jd_model, in_tok, out_tok)
                telemetry.set_generation(_gen0, input_tokens=in_tok, output_tokens=out_tok,
                                         output={"role_title": jd.role_title,
                                                 "seniority_level": jd.seniority_level,
                                                 "required_keywords": rubric.required_keywords})
            telemetry.set_metadata(_sp0, role_title=jd.role_title, seniority_level=jd.seniority_level)
        ctx.write_checkpoint("phase0_jd_analysis", jd)
        ctx.write_checkpoint("phase0_rubric", rubric)
        ctx.audit.log_event("phase0", "jd_analysed", f"{jd.role_title} ({jd.seniority_level})",
                            rubric_version=rubric.version)
        emit("phase_complete", phase="phase0_jd_analysis",
             role_title=jd.role_title, seniority=jd.seniority_level)

        # Phase 1 — fit assessment (RAG + Claude)
        emit("phase_start", phase="phase1_fit_assessment", label="Fit assessment")
        sections = all_sections(config)
        ctx.audit.log_event("phase1", "cvcm", "value model loaded" if cvcm else "no value model (optional)")
        with telemetry.span("phase1_fit_assessment") as _sp1:
            # The fit-assessment Claude call traces itself as a generation inside claude_complete,
            # nesting here automatically; this span just carries the phase-level verdict metadata.
            fit, _ = assess_fit(jd, rubric, model=rc.orchestrator_model, config=config,
                                sections=sections, cvcm=cvcm)
            telemetry.set_metadata(_sp1, outcome=fit.outcome,
                                   overall_fit_score=round(fit.overall_fit_score, 4),
                                   cvcm_enabled=cvcm is not None)
        ctx.write_checkpoint("phase1_fit_assessment", fit)
        emit("phase_complete", phase="phase1_fit_assessment",
             outcome=fit.outcome, fit_score=round(fit.overall_fit_score, 3), hitl_required=True)
        proceed = hitl.fit(fit, jd)

        if dry_run:
            summary["outcome"] = fit.outcome
            summary["dry_run"] = True
            _finalise(ctx, tracker, rc, iterations_run=0)
            emit("run_complete", run_id=ctx.run_id, outcome=fit.outcome, dry_run=True)
            return summary
        if not proceed:
            ctx.audit.log_event("phase1", "stopped_by_human",
                                f"human stopped at fit ({fit.outcome})")
            _finalise(ctx, tracker, rc, iterations_run=0)
            emit("stopped", phase="phase1_fit_assessment", outcome=fit.outcome)
            raise PipelineStop(f"stopped at fit assessment (outcome: {fit.outcome})")

        # Phase 2 — initial draft (Claude)
        emit("phase_start", phase="phase2_initial_draft", label="Initial draft")
        with telemetry.span("phase2_initial_draft") as _sp2:
            manifest = draft_sections(fit, jd, rubric, sections, budgets, ctx,
                                      model=rc.orchestrator_model, cvcm=cvcm)
            telemetry.set_metadata(_sp2, sections=len(manifest))
        emit("phase_complete", phase="phase2_initial_draft", sections=len(manifest))

        # Phase 3 — dual-writer refinement loop (emits per-section + per-iteration events)
        emit("phase_start", phase="phase3_refinement", label="Refinement loop",
             max_iterations=rc.max_iterations)
        with telemetry.span("phase3_refinement") as _sp3:
            # refine() opens a child span per iteration; the writer/orchestrator generations
            # (via claude_complete/gpt_complete) nest under those automatically.
            result = refine(manifest, jd, rubric, budgets, ctx,
                            model=rc.orchestrator_model, gpt_model=rc.gpt_model,
                            max_iterations=rc.max_iterations,
                            keyword_delta_threshold=rc.keyword_delta_threshold,
                            critique_delta_threshold=rc.critique_delta_threshold,
                            max_rubric_additions=rc.max_rubric_additions,
                            on_event=on_event, cvcm=cvcm)
            telemetry.set_metadata(_sp3, iterations_run=len(result.iterations),
                                   converged=result.converged,
                                   convergence_reason=result.convergence_reason)
        rubric = result.final_rubric
        emit("phase_complete", phase="phase3_refinement",
             converged=result.converged, convergence_reason=result.convergence_reason,
             iterations=len(result.iterations))

        # Phase 4 — verification gate (F-35) + human review (HITL).
        # Ground each tailored section against the RAW corpus source and flag any
        # unsupported claim as a major review item, so fabrication is shown to the human
        # (and recorded for the report) before anything ships — never silently.
        emit("phase_start", phase="phase4_hitl", label="Human review")
        # The grounding check (Haiku verifier) is the traced unit — its generations nest in this
        # span. Human review (hitl.review) is left untraced: it can block on a person indefinitely.
        with telemetry.span("phase4_grounding") as _sp4:
            flags = verifier.verify_run(ctx, result.manifest, model=rc.validation_model)
            flag_count = _merge_verification_flags(ctx, result, flags)
            grounded_coverage = result.iterations[-1].keyword_coverage if result.iterations else None
            telemetry.set_metadata(_sp4, fabrication_flags=flag_count,
                                   grounded_coverage=grounded_coverage)
        hitl.review(ctx, result, jd, rubric, budgets, rc)
        emit("phase_complete", phase="phase4_hitl", fabrication_flags=flag_count)

        # Phase 5 — formatting validation (Haiku) + assembled length check
        emit("phase_start", phase="phase5_validation", label="Formatting")
        with telemetry.span("phase5_validation") as _sp5:
            corrections = phase5_validation.validate_formatting(ctx, result.manifest,
                                                                model=rc.validation_model)
            length = phase5_validation.assembled_length_check(result.manifest, budgets)
            telemetry.set_metadata(_sp5, corrections=len(corrections))
        if corrections and hitl.formatting(corrections, length):
            phase5_validation.apply_corrections(ctx, corrections, result.manifest)
        emit("phase_complete", phase="phase5_validation", corrections=len(corrections))

        # Phase 6 — output generation (+ cv_final.docx for --docx, formatting from a
        # source CV in the corpus; D-13 static text is the person's own, so the source
        # CV's look is the right reference — the stretch is clean CV only).
        emit("phase_start", phase="phase6_output", label="Output generation")
        source_docx = None
        if docx:
            from tailor.phases import phase6_docx
            source_docx = phase6_docx.resolve_template(result.manifest)
            if source_docx is None:
                ctx.audit.log_event("phase6_output", "docx_skipped",
                                    "no source .docx in data/cvs/; --docx skipped")
        # Persist the FINAL manifest (versions updated through refinement + any applied
        # formatting corrections). The Phase-2 manifest checkpoint is the pre-refinement
        # state (versions all 0); without this the run dir isn't self-describing and a report
        # can't be regenerated faithfully from disk (F-40).
        ctx.write_checkpoint("final_manifest", result.manifest)
        with telemetry.span("phase6_output") as _sp6:
            out = generate_output(ctx, result.manifest, jd, fit, rubric, result.iterations,
                                  config=config, source_docx=source_docx, verification_flags=flags,
                                  jd_raw=jd_text)
            telemetry.set_metadata(_sp6, sections=len(result.manifest), docx=bool(out.get("docx")))
        emit("phase_complete", phase="phase6_output")

        footer = _finalise(ctx, tracker, rc, iterations_run=len(result.iterations))
        summary.update({
            "outcome": fit.outcome,
            "converged": result.converged,
            "convergence_reason": result.convergence_reason,
            "iterations": len(result.iterations),
            # cv_html dropped in Phase 3 — the report HTML is regenerated on demand, not at run
            # time (SPEC_SQLITE_MIGRATION §6). cv_final.md stays as the submission artefact.
            "cv_md": out["md"], "cv_docx": out.get("docx"),
            "fabrication_flags": flag_count,
            "cost_estimated_usd": footer["total_estimated_usd"],
            "cost_breakdown": footer["cost_breakdown_estimated_usd"],
        })
        if rc.cost_cap_usd and footer["total_estimated_usd"] > rc.cost_cap_usd:
            summary["cost_cap_exceeded"] = True
        # grounded_coverage (final source-grounded keyword coverage, F-38) + fabrication_flags
        # (F-35) ride the footer so the archive can show the summary card without re-deriving.
        grounded_coverage = result.iterations[-1].keyword_coverage if result.iterations else None
        summary["grounded_coverage"] = grounded_coverage
        # SQLite run store (SPEC_SQLITE_MIGRATION §3): record the completed run's structured
        # metadata from its on-disk checkpoints, alongside — never replacing — the JSONL/disk
        # source of truth. Secondary store: a DB failure must not break a run, so guard it.
        _record_sqlite(ctx.run_id, output_dir, summary)
        emit("run_complete", run_id=ctx.run_id, outcome=fit.outcome,
             converged=result.converged, convergence_reason=result.convergence_reason,
             iterations=len(result.iterations), grounded_coverage=grounded_coverage,
             fabrication_flags=flag_count,
             cost_estimated_usd=footer["total_estimated_usd"],
             cost_breakdown=footer["cost_breakdown_estimated_usd"])
    return summary


def _merge_verification_flags(ctx, result, flags: dict) -> int:
    """Fold verifier flags into the review's unresolved items (so the human sees them,
    even on a section that converged) and log them. Returns the total flag count (F-35)."""
    total = 0
    for sid, items in flags.items():
        bucket = result.unresolved.setdefault(sid, [])
        existing = {it.issue for it in bucket}
        # Fabrication flags go FIRST — they're the most important thing to address.
        result.unresolved[sid] = [it for it in items if it.issue not in existing] + bucket
        for it in items:
            ctx.audit.log_event("verification", "unsupported_claim", f"{sid}: {it.issue}")
        total += len(items)
    if total:
        ctx.audit.log_event("verification", "flags_raised",
                            f"{total} unsupported claim(s) across {len(flags)} section(s) — surfaced for review")
    else:
        ctx.audit.log_event("verification", "all_grounded",
                            "every tailored section traces to the source CV")
    return total


def _finalise(ctx, tracker, rc, *, iterations_run: int) -> dict:
    """Write the run_complete cost footer to run_log.jsonl (§9)."""
    footer = tracker.footer(mode=rc.mode, iterations_run=iterations_run)
    ctx.audit.log_footer(footer)
    return footer


def _record_sqlite(run_id: str, output_dir: str, summary: dict) -> None:
    """Record the completed run to the SQLite store (SPEC_SQLITE_MIGRATION §3), best-effort.

    The DB is a secondary, queryable view over the same on-disk checkpoints; the JSONL +
    checkpoint files remain authoritative. A write failure (locked DB, disk full) must never
    fail an otherwise-complete run, so swallow and log it — exactly like the audit/telemetry
    side channels. The migration script backfills anything missed."""
    try:
        db.record_run_complete(run_id, output_dir=output_dir, summary=summary)
    except Exception:                       # pragma: no cover — defensive, like telemetry
        logging.getLogger("cv_tailor.run").warning("SQLite record failed for run %s", run_id,
                                                    exc_info=True)
