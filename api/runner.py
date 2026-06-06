"""api/runner.py — run the pipeline in a background thread, streaming to a Session.

`run_pipeline` is blocking and emits progress via its `on_event` hook; we run it off
the event loop in a daemon thread and feed every event straight into the session's
buffer (which wakes the SSE stream). Durable artifacts still land in outputs/<run_id>/
exactly as a CLI run; the session only carries the live event stream + final summary.

Two HITL handlers share the pipeline's handler interface (fit / review / formatting):

- **AutoHITL** (from `tailor.run`) — accepts every checkpoint; the run goes
  start-to-finish. Used for `?auto=true` (the quick demo path) and tests.
- **SSEHITL** (here, UI Step 4) — each checkpoint publishes a JSON payload via
  `Session.wait_hitl` and BLOCKS until the human POSTs to `/api/runs/{id}/hitl`.
  Free-text is interpreted by Haiku and shown back before applying (preview-before-
  apply, D-18), exactly as the CLI's TerminalHITL does — only the front end differs.
"""

from __future__ import annotations

import threading

from tailor.config import cv_display_name, load_config, resolve_run_config
from tailor.phases import phase1_fit_assessment, phase4_hitl
from tailor.run import AutoHITL, PipelineStop, run_pipeline

__all__ = ["launch_run", "SSEHITL"]


# --------------------------------------------------------------------------- #
# Checkpoint payloads — JSON-safe snapshots for the front end (§12.3)         #
# --------------------------------------------------------------------------- #

def _section_label(manifest: dict, sid: str) -> str:
    m = manifest.get(sid, {})
    return m.get("label") or m.get("title") or sid


def fit_payload(fit, jd) -> dict:
    """The fit-assessment checkpoint as a dict (FitAssessment has dataclass fields)."""
    mix = None
    if fit.recommended_sections:
        cfg = load_config()
        mix = [
            # source_cv is shown to the user → display the company-name-free label (F-41)
            {"section_id": sid, "source_cv": cv_display_name(cfg, r.source_cv),
             "coverage": round(r.keyword_coverage, 3), "reason": r.reason,
             "static": r.reason.startswith("static")}
            for sid, r in sorted(fit.recommended_sections.items())
        ]
    return {
        "role_title": getattr(jd, "role_title", ""),
        "company": getattr(jd, "company_context", ""),
        "outcome": fit.outcome,
        "fit_score": round(fit.overall_fit_score, 3),
        "no_fit_reason": fit.no_fit_reason,
        "value_alignment_notes": getattr(fit, "value_alignment_notes", None),
        "skills_transferable": list(fit.skills_transferable),
        "gaps": [{"requirement": g.requirement, "gap_type": g.gap_type, "severity": g.severity,
                  "addressable": g.addressable, "reason": g.reason} for g in fit.gaps],
        "section_mix": mix,
        "options": (["override", "stop"] if fit.outcome == "no_fit" else ["proceed", "stop"]),
    }


def review_payload(result, rc, *, preview: dict | None = None) -> dict:
    """The section-review checkpoint: progression, per-section status, unresolved items."""
    its = result.iterations
    sections = []
    for sid in sorted(result.manifest):
        m = result.manifest[sid]
        label = _section_label(result.manifest, sid)
        if m["static"]:
            sections.append({"section_id": sid, "label": label, "status": "static",
                             "version": m.get("version")})
            continue
        citer = phase4_hitl.converged_at(result, sid)
        sections.append({"section_id": sid, "label": label,
                         "status": "converged" if citer is not None else "active",
                         "converged_iter": citer, "version": m.get("version")})
    unresolved = [
        {"index": i, "section_id": sid, "label": _section_label(result.manifest, sid),
         "issue": it.issue, "severity": it.severity, "suggestion": it.suggestion}
        for i, (sid, it) in enumerate(phase4_hitl.unresolved_list(result), 1)
    ]
    return {
        "convergence_reason": result.convergence_reason,
        "iterations": len(its),
        "max_iterations": rc.max_iterations,
        "keyword_coverage": [round(it.keyword_coverage, 3) for it in its],
        "quality": [None if it.critique_score is None else round(it.critique_score, 2) for it in its],
        "sections": sections,
        "unresolved": unresolved,
        "preview": preview,        # a pending Haiku interpretation awaiting confirm-apply, or None
    }


def formatting_payload(corrections: dict, length: dict) -> dict:
    """The formatting checkpoint: per-section fixes + the assembled-length envelope."""
    return {
        "corrections": [
            {"section_id": sid, "label": sid, "fixes": list(c["corrections"]),
             "original": c["original"], "corrected": c["corrected"]}
            for sid, c in corrections.items()
        ],
        "length": length,
    }


# --------------------------------------------------------------------------- #
# SSEHITL — pause at each checkpoint until the human responds over HTTP        #
# --------------------------------------------------------------------------- #

class SSEHITL:
    """Conversational HITL over the Session handoff. Runs on the pipeline thread:
    each method publishes a checkpoint payload and blocks on `wait_hitl` until the
    `/hitl` endpoint calls `submit_hitl`. Free-text → Haiku (validation model) → shown
    back before applying; revisions reuse phase4's claude_writer pass (D-18)."""

    def __init__(self, session, *, validation_model: str):
        self.session = session
        self.validation_model = validation_model

    # -- Checkpoint 1: fit assessment (§12.3) ------------------------------- #
    def fit(self, fit, jd) -> bool:
        resp = self.session.wait_hitl("fit_assessment", fit_payload(fit, jd))
        action = (resp.get("action") or "").strip().lower()
        if action == "freetext":
            try:
                dec = phase1_fit_assessment.interpret_fit_response(
                    resp.get("text", ""), fit, model=self.validation_model)
            except phase1_fit_assessment.FitAssessmentError as exc:
                self.session.add_event({"type": "hitl_error", "checkpoint": "fit_assessment",
                                        "message": str(exc)})
                return self.fit(fit, jd)        # re-publish, ask again
            action = dec["action"]
            self.session.add_event({"type": "hitl_interpreted", "checkpoint": "fit_assessment",
                                    "action": action, "reason": dec.get("reason", "")})
        if fit.outcome == "no_fit":
            return action in ("override", "proceed")
        return action != "stop"

    # -- Checkpoint 2: section review (§12.3) — multi-turn loop -------------- #
    def review(self, ctx, result, jd, rubric, budgets, rc) -> None:
        preview: dict | None = None
        while True:
            resp = self.session.wait_hitl("section_review", review_payload(result, rc, preview=preview))
            preview = None
            action = (resp.get("action") or "").strip().lower()
            try:
                if action in ("", "accept", "done"):
                    return
                if action == "apply_item":
                    self._apply_item(int(resp.get("index", 0)), result, jd, rubric, budgets, ctx, rc)
                elif action == "interpret":              # free text → Haiku → preview (no apply yet)
                    dec = phase4_hitl.interpret_freetext(resp.get("text", ""), result,
                                                         model=rc.validation_model)
                    preview = {"section_id": dec["section_id"], "instruction": dec["instruction"],
                               "label": _section_label(result.manifest, dec["section_id"])}
                    self.session.add_event({"type": "hitl_interpreted", "checkpoint": "section_review",
                                            **preview})
                elif action == "apply_freetext":         # confirm-apply a previewed interpretation
                    self._revise(resp.get("section_id"), resp.get("instruction"),
                                 result, jd, rubric, budgets, ctx, rc)
                else:
                    self.session.add_event({"type": "hitl_error", "checkpoint": "section_review",
                                            "message": f"unknown action {action!r}"})
            except (phase4_hitl.HITLError, ValueError, IndexError) as exc:
                self.session.add_event({"type": "hitl_error", "checkpoint": "section_review",
                                        "message": str(exc)})

    def _apply_item(self, n, result, jd, rubric, budgets, ctx, rc) -> None:
        items = phase4_hitl.unresolved_list(result)
        sid, it = items[n - 1]                            # 1-based; raises IndexError if bad
        self._revise(sid, it.suggestion, result, jd, rubric, budgets, ctx, rc)
        result.unresolved.get(sid, []).remove(it)         # drop the resolved item from the list

    def _revise(self, sid, instruction, result, jd, rubric, budgets, ctx, rc) -> None:
        v, _ = phase4_hitl.revise_section(sid, instruction, result, jd, rubric, budgets, ctx,
                                          model=rc.orchestrator_model)
        self.session.add_event({"type": "hitl_applied", "checkpoint": "section_review",
                                "section_id": sid, "label": _section_label(result.manifest, sid),
                                "version": v, "instruction": instruction})

    # -- Checkpoint 3: formatting (§12.3) — binary Approve/Reject ----------- #
    def formatting(self, corrections, length) -> bool:
        resp = self.session.wait_hitl("formatting", formatting_payload(corrections, length))
        return (resp.get("action") or "").strip().lower() in ("approve", "apply", "yes", "y")


# --------------------------------------------------------------------------- #
# Launch                                                                       #
# --------------------------------------------------------------------------- #

def launch_run(store, session, jd_text, *, mode="demo", key=None, max_iterations=None,
               output_dir="outputs", auto=False) -> threading.Thread:
    """Write the JD to the session's tmp dir and run the pipeline in a daemon thread.

    `auto=True` uses AutoHITL (start-to-finish, the quick demo path); otherwise SSEHITL
    pauses at each checkpoint for the human (UI Step 4). Terminal status is set by the
    thread: complete / stopped (PipelineStop, e.g. no_fit) / error. run_pipeline emits
    its own run_complete / stopped event before returning/raising, so the SSE sees it."""
    jd_path = store.base_dir / session.run_id / "jd.txt"
    jd_path.parent.mkdir(parents=True, exist_ok=True)
    jd_path.write_text(jd_text, encoding="utf-8")

    if auto:
        hitl = AutoHITL()
    else:
        rc = resolve_run_config(load_config(), mode=mode, key=key, max_iterations=max_iterations)
        hitl = SSEHITL(session, validation_model=rc.validation_model)

    def target() -> None:
        try:
            session.set_status("running")
            summary = run_pipeline(
                str(jd_path), mode=mode, key=key, max_iterations=max_iterations,
                output_dir=output_dir, hitl=hitl, run_id=session.run_id,
                on_event=session.add_event,
            )
            session.result = summary
            session.set_status("complete")
        except PipelineStop as exc:                # no_fit / human stop
            session.error = str(exc)
            session.set_status("stopped")          # run_pipeline already emitted 'stopped'
        except Exception as exc:                   # surface any failure to the stream
            session.error = str(exc)
            session.add_event({"type": "error", "message": str(exc)})
            session.set_status("error")

    thread = threading.Thread(target=target, name=f"run-{session.run_id}", daemon=True)
    thread.start()
    return thread
