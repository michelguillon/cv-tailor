"""tailor/run.py — pipeline assembly: Phase 0 → 6, HITL, cost footer. Step 8.

The deterministic scaffold around the agentic loop (D-01). `run_pipeline` sequences
the phases, pausing at the three HITL checkpoints (fit / section review /
formatting). The checkpoints are delegated to a HITL handler so the same pipeline
serves the interactive CLI (`TerminalHITL`) and tests / non-interactive runs
(`AutoHITL`) — the phases only ever *render*; the input lives here (like Phase 1's
render_fit_hitl). Cost is captured centrally via cost.track() (D-08, estimates F-08).
"""

from __future__ import annotations

from pathlib import Path

from corpus.retrieval import all_sections
from tailor import cost
from tailor.config import load_budgets, load_config, resolve_run_config
from tailor.phases import phase4_hitl, phase5_validation
from tailor.phases.phase0_jd_analysis import analyse_jd
from tailor.phases.phase1_fit_assessment import assess_fit, render_fit_hitl
from tailor.phases.phase2_initial_draft import draft_sections
from tailor.phases.phase3_refinement import refine
from tailor.phases.phase6_output import generate_output
from tailor.run_context import RunContext

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
                 on_event=None) -> dict:
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

    summary = {"run_id": ctx.run_id, "mode": rc.mode, "output_dir": str(ctx.output_dir)}

    with cost.track() as tracker:
        # Phase 0 — JD analysis (Mistral). This one provider call doesn't go through a
        # cost-noting helper (it uses the Mistral client directly), so note its usage
        # here. Phase 1+ go through claude_complete/gpt_complete, which note themselves.
        emit("phase_start", phase="phase0_jd_analysis", label="JD analysis")
        jd, rubric, jd_usage = analyse_jd(jd_text, model=rc.jd_model)
        if jd_usage is not None:
            cost.note(rc.jd_model, getattr(jd_usage, "prompt_tokens", 0) or 0,
                      getattr(jd_usage, "completion_tokens", 0) or 0)
        ctx.write_checkpoint("phase0_jd_analysis", jd)
        ctx.write_checkpoint("phase0_rubric", rubric)
        ctx.audit.log_event("phase0", "jd_analysed", f"{jd.role_title} ({jd.seniority_level})",
                            rubric_version=rubric.version)
        emit("phase_complete", phase="phase0_jd_analysis",
             role_title=jd.role_title, seniority=jd.seniority_level)

        # Phase 1 — fit assessment (RAG + Claude)
        emit("phase_start", phase="phase1_fit_assessment", label="Fit assessment")
        sections = all_sections(config)
        fit, _ = assess_fit(jd, rubric, model=rc.orchestrator_model, config=config, sections=sections)
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
        manifest = draft_sections(fit, jd, rubric, sections, budgets, ctx,
                                  model=rc.orchestrator_model)
        emit("phase_complete", phase="phase2_initial_draft", sections=len(manifest))

        # Phase 3 — dual-writer refinement loop (emits per-section + per-iteration events)
        emit("phase_start", phase="phase3_refinement", label="Refinement loop",
             max_iterations=rc.max_iterations)
        result = refine(manifest, jd, rubric, budgets, ctx,
                        model=rc.orchestrator_model, gpt_model=rc.gpt_model,
                        max_iterations=rc.max_iterations,
                        keyword_delta_threshold=rc.keyword_delta_threshold,
                        critique_delta_threshold=rc.critique_delta_threshold,
                        max_rubric_additions=rc.max_rubric_additions,
                        on_event=on_event)
        rubric = result.final_rubric
        emit("phase_complete", phase="phase3_refinement",
             converged=result.converged, convergence_reason=result.convergence_reason,
             iterations=len(result.iterations))

        # Phase 4 — human review (HITL)
        emit("phase_start", phase="phase4_hitl", label="Human review")
        hitl.review(ctx, result, jd, rubric, budgets, rc)
        emit("phase_complete", phase="phase4_hitl")

        # Phase 5 — formatting validation (Haiku) + assembled length check
        emit("phase_start", phase="phase5_validation", label="Formatting")
        corrections = phase5_validation.validate_formatting(ctx, result.manifest,
                                                            model=rc.validation_model)
        length = phase5_validation.assembled_length_check(result.manifest, budgets)
        if corrections and hitl.formatting(corrections, length):
            phase5_validation.apply_corrections(ctx, corrections, result.manifest)
        emit("phase_complete", phase="phase5_validation", corrections=len(corrections))

        # Phase 6 — output generation
        emit("phase_start", phase="phase6_output", label="Output generation")
        out = generate_output(ctx, result.manifest, jd, fit, rubric, result.iterations, config=config)
        emit("phase_complete", phase="phase6_output")

        footer = _finalise(ctx, tracker, rc, iterations_run=len(result.iterations))
        summary.update({
            "outcome": fit.outcome,
            "converged": result.converged,
            "convergence_reason": result.convergence_reason,
            "iterations": len(result.iterations),
            "cv_md": out["md"], "cv_html": out["html"],
            "cost_estimated_usd": footer["total_estimated_usd"],
            "cost_breakdown": footer["cost_breakdown_estimated_usd"],
        })
        if rc.cost_cap_usd and footer["total_estimated_usd"] > rc.cost_cap_usd:
            summary["cost_cap_exceeded"] = True
        emit("run_complete", run_id=ctx.run_id, outcome=fit.outcome,
             converged=result.converged, convergence_reason=result.convergence_reason,
             iterations=len(result.iterations),
             cost_estimated_usd=footer["total_estimated_usd"],
             cost_breakdown=footer["cost_breakdown_estimated_usd"])
    return summary


def _finalise(ctx, tracker, rc, *, iterations_run: int) -> dict:
    """Write the run_complete cost footer to run_log.jsonl (§9)."""
    footer = tracker.footer(mode=rc.mode, iterations_run=iterations_run)
    ctx.audit.log_footer(footer)
    return footer
