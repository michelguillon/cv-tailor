"""Phase 3 — Dual-writer refinement loop (the agentic region, D-01/D-28). Step 6.

Input:  the Phase 2 manifest (section_id → {static, version, source_cv, path,
        section_type}), JDAnalysis, ScoringRubric, budgets, RunContext.
Output: versioned section files (<id>_v<n>.md = the selected draft; plus per-writer
        <id>_<writer>_v<n>.md for inspection), updated ScoringRubric, a
        list[IterationScore], per-iteration checkpoints. Reasoning → run_log.jsonl
        (D-06), never fed back into context.
Models: Claude (writer + orchestrator) + GPT-4o-mini (writer).

This replaces the single-writer critique loop (D-28): two independent writers
draft every active section, a Claude orchestrator adjudicates (select / synthesise
/ direct), and both writers get one pushback exchange (D-29). Per active section,
per iteration:

  1. Dual write   — claude_writer + gpt_writer each draft the section, self-flagging
     issues (the canonical source for the zero-major freeze, D-28).
  2. Adjudicate   — orchestrator scores both, picks/synthesises, sets direction,
     judges convergence; selected text → <id>_v<n>.md.
  3. Pushback     — both writers may object once; orchestrator holds/revises the
     direction carried into the next iteration (D-29).
  4. Freeze       — section frozen when the orchestrator converged it AND zero major
     items remain across both drafts; excluded from later iterations.
  5. Score        — per-section + aggregate IterationScore with deltas.

After the per-section pass: pooled rubric additions are JD-validated (max 2/iter,
D-04, tools/rubric.py), then dual-signal convergence / soft-stop / max_iterations
(D-05, thresholds confirmed F-16). LoopMemory (directions, rejected minor
suggestions, frozen set, score history) is forwarded each iteration as structured
state — a shared whiteboard, not prose reasoning (D-30, D-06-compatible).

Aggregate keyword_coverage is SOURCE-GROUNDED UNION coverage across non-static
sections (F-15 + F-38): a keyword counts only where the candidate's raw corpus
supports it, so fabricated keywords never register as coverage gained (the Goodhart
fix — the metric stops rewarding what the writer rules already forbid). Aggregate
quality is the mean of the SELECTED draft's quality across active sections, so it
can dip as easy sections freeze — absorbed by the 0.5 threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from tailor.models import IterationScore, ScoringRubric, SectionScore, WriterDraft
from tailor.tools import claude_writer, gpt_writer, orchestrator_tool
from tailor.tools.gpt_writer import WriterError
from tailor.tools.rubric import validate_rubric_additions
from tailor.tools.scorer import keyword_coverage, union_coverage

__all__ = ["refine", "RefinementResult", "LoopMemory"]


@dataclass
class LoopMemory:
    """Structured state forwarded between iterations (D-30) — not prose reasoning."""
    rejected_suggestions: list[str] = field(default_factory=list)   # deduped MINOR suggestions (F-19)
    directions: dict[str, str] = field(default_factory=dict)        # section_id → current direction
    orchestrator_directions: list[str] = field(default_factory=list)  # flat log, one per (iter, section)
    frozen_sections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rejected_suggestions": self.rejected_suggestions,
            "directions": self.directions,
            "orchestrator_directions": self.orchestrator_directions,
            "frozen_sections": self.frozen_sections,
        }


@dataclass
class RefinementResult:
    iterations: list[IterationScore]
    final_rubric: ScoringRubric
    converged: bool
    convergence_reason: str
    manifest: dict
    memory: LoopMemory
    # section_id → open writer items for sections that never converged (Phase 4 HITL)
    unresolved: dict = field(default_factory=dict)


def _selected_quality(decision) -> float:
    """Quality of the draft the orchestrator chose. Synthesis ≥ both, so take the max."""
    if decision.selected_base == "claude":
        return decision.claude_quality
    if decision.selected_base == "gpt":
        return decision.gpt_quality
    return max(decision.claude_quality, decision.gpt_quality)


def _current_text(ctx, manifest, sid) -> str:
    m = manifest[sid]
    if m["static"]:
        return ctx.read_section(sid, static=True)
    return ctx.read_section(sid, version=m["version"])


def _raw_source(ctx, sid, fallback: str) -> str:
    """The raw corpus text for a section (the truth ground the orchestrator checks
    against, F-35). Falls back to the current draft if no source file was persisted
    (older runs / unit tests that don't go through Phase 2)."""
    if ctx.has_section(sid, source=True):
        return ctx.read_section(sid, source=True)
    return fallback


def _aggregate_keyword_coverage(ctx, manifest, rubric) -> float:
    """UNION coverage across non-static sections — SOURCE-GROUNDED (F-38): a keyword
    counts only where the candidate's raw corpus supports it, so fabricated keywords
    add nothing to the convergence signal. Sources fall back to the draft when no
    Phase-2 source was persisted (older runs / unit tests), preserving old behaviour."""
    items = [(sid, _current_text(ctx, manifest, sid))
             for sid, m in manifest.items() if not m["static"]]
    if not items:
        return 0.0
    texts = [t for _, t in items]
    sources = [_raw_source(ctx, sid, t) for sid, t in items]
    return round(union_coverage(texts, rubric, source_texts=sources), 4)


def _write_writer_draft(ctx, draft):
    """Per-writer draft on disk for the Changes tab / inspection (not load-bearing)."""
    path = ctx.sections_dir / f"{draft.section_id}_{draft.writer}_v{draft.version}.md"
    path.write_text(draft.text.rstrip() + "\n", encoding="utf-8")


def _safe_write(write_fn, sid: str, writer: str, *, audit, iteration: int):
    """Run a writer, returning its draft or None on ANY failure (F-39). A transient
    provider/parse failure must not abort the whole run — the loop degrades to the
    surviving writer. The failure is logged loudly (a systemic bug shows up as
    writer_failed on every section, so it isn't silently swallowed)."""
    try:
        return write_fn()
    except Exception as exc:   # noqa: BLE001 — resilience is the point; detail goes to the audit log
        audit.log_event("refinement", "writer_failed",
                        f"{sid}: {writer}_writer raised {type(exc).__name__}: {exc}",
                        iteration=iteration)
        return None


def refine(
    manifest: dict,
    jd,
    rubric: ScoringRubric,
    budgets: dict,
    ctx,
    *,
    model: str,
    gpt_model: str = "gpt-4o-mini",
    max_iterations: int = 1,
    keyword_delta_threshold: float = 0.05,
    critique_delta_threshold: float = 0.5,
    max_rubric_additions: int = 2,
    claude_client=None,
    openai_client=None,
    on_event=None,
    cvcm: str | None = None,
) -> RefinementResult:
    """Run the dual-writer refinement loop. See the module docstring for the flow.

    `on_event` (optional) is called with a progress dict per adjudicated section and
    per completed iteration — the Web UI SSE hook (SPEC §12.2). No-op for the CLI."""
    def emit(type_, **fields):
        if on_event is not None:
            on_event({"type": type_, **fields})

    audit = ctx.audit
    section_types = {sid: m["section_type"] for sid, m in manifest.items()}
    nonstatic = [sid for sid, m in manifest.items() if not m["static"]]
    frozen: dict[str, int] = {}
    mem = LoopMemory()

    iterations: list[IterationScore] = []
    latest_items: dict[str, list] = {}    # section_id → last iteration's writer items
    prev_keyword = _aggregate_keyword_coverage(ctx, manifest, rubric)
    prev_quality: float | None = None
    converged, reason = False, ""

    audit.log_event("refinement", "loop_start",
                    f"dual-writer; {len(nonstatic)} active sections; max_iterations={max_iterations}",
                    keyword_score=prev_keyword, rubric_version=rubric.version)

    for n in range(1, max_iterations + 1):
        active = [sid for sid in nonstatic if sid not in frozen]
        if not active:
            converged, reason = True, "all_sections_converged"
            break
        is_final = (n == max_iterations)

        section_scores: dict[str, SectionScore] = {}
        proposed_additions: list[str] = []
        major_total = 0
        newly_frozen = 0

        for sid in active:
            stype = section_types[sid]
            budget = budgets.get(stype)
            current = _current_text(ctx, manifest, sid)
            direction = mem.directions.get(sid)

            # 1. dual write — each writer is wrapped so a transient provider failure
            #    DEGRADES to the surviving writer instead of aborting the whole run (F-39).
            cd = _safe_write(lambda: claude_writer.write_section(
                sid, current, jd, rubric, budget, version=n, direction=direction,
                rejected_suggestions=mem.rejected_suggestions, is_final=is_final,
                model=model, client=claude_client, cvcm=cvcm), sid, "claude", audit=audit, iteration=n)
            gd = _safe_write(lambda: gpt_writer.write_section(
                sid, current, jd, rubric, budget, version=n, direction=direction,
                rejected_suggestions=mem.rejected_suggestions, is_final=is_final,
                model=gpt_model, client=openai_client, cvcm=cvcm), sid, "gpt", audit=audit, iteration=n)

            # If exactly one writer failed, stand in the survivor's text for the failed one so
            # the orchestrator still scores/directs the section; the selected text is forced
            # back to the survivor verbatim below (no synthesis drift). Both failing is a real
            # outage for this section — surface it (R-09), don't ship a blank section.
            degraded = None
            if cd is None and gd is None:
                raise WriterError(
                    f"both writers failed for {sid} at iteration {n} — cannot draft this section")
            if cd is None or gd is None:
                survivor = cd or gd
                degraded = "gpt" if gd is None else "claude"
                standin = WriterDraft(writer=degraded, section_id=sid, text=survivor.text,
                                      version=n, items=[])
                cd, gd = (survivor, standin) if degraded == "gpt" else (standin, survivor)
                audit.log_event("refinement", "writer_degraded",
                                f"{sid}: {degraded}_writer failed; proceeding with "
                                f"{survivor.writer}-only (degraded confidence)", iteration=n)
                emit("writer_degraded", section_id=sid, iteration=n, failed=degraded,
                     survivor=survivor.writer)
            _write_writer_draft(ctx, cd)
            _write_writer_draft(ctx, gd)

            # 2. adjudicate → write selected text as v(n). Ground the orchestrator on the
            #    RAW corpus source (F-35), not the evolving draft, so fabrication that
            #    originated in Phase 2 or drifted in earlier iterations is still caught.
            prior = section_scores.get(sid)
            decision, selected_text = orchestrator_tool.adjudicate(
                sid, cd, gd, rubric, jd, source_text=_raw_source(ctx, sid, current),
                cvcm=cvcm, prior_score=prior, is_final=is_final, model=model, client=claude_client)
            if degraded:
                # Only one writer really drafted — use its text verbatim (the orchestrator
                # compared it against a copy of itself, so its scores/direction are valid, but
                # we don't want a synthesised reword of a single source). Label honestly.
                survivor_base = "gpt" if degraded == "claude" else "claude"
                selected_text = (gd if degraded == "claude" else cd).text
                decision = replace(decision, selected_base=survivor_base)
            ctx.write_section(sid, selected_text, version=n)
            manifest[sid]["version"] = n
            manifest[sid]["word_count"] = len(selected_text.split())
            audit.log_event("refinement", "section_adjudicated",
                            f"{sid} → v{n}: base={decision.selected_base} "
                            f"(claude {decision.claude_quality}/gpt {decision.gpt_quality}); "
                            f"direction: {decision.direction}",
                            iteration=n, keyword_score=decision.keyword_coverage,
                            critique_score=_selected_quality(decision), rubric_version=rubric.version)

            # 3. pushback (one exchange; skip on the final pass). Skip the FAILED writer's
            #    pushback when degraded — it would just fail again (F-39); the survivor's
            #    pushback still runs so the direction can still be revised.
            new_direction = decision.direction
            if not is_final:
                cp = (claude_writer.pushback(sid, decision, cd, jd, model=model, client=claude_client)
                      if degraded != "claude" else None)
                gp = (gpt_writer.pushback(sid, decision, gd, jd, model=gpt_model, client=openai_client)
                      if degraded != "gpt" else None)
                if cp or gp:
                    new_direction = orchestrator_tool.read_pushbacks(
                        sid, decision, cp, gp, jd, model=model, client=claude_client)
                    audit.log_event("refinement", "pushback_resolved",
                                    f"{sid}: claude={cp or 'agreed'} | gpt={gp or 'agreed'} "
                                    f"→ direction: {new_direction}",
                                    iteration=n, rubric_version=rubric.version)
            mem.directions[sid] = new_direction
            mem.orchestrator_directions.append(f"iter{n} {sid}: {new_direction}")

            # 4. freeze: orchestrator converged AND zero major items across both drafts
            latest_items[sid] = cd.items + gd.items
            majors = [it for it in latest_items[sid] if it.severity == "major"]
            major_total += len(majors)
            do_freeze = decision.converged and not majors
            if do_freeze:
                frozen[sid] = n
                newly_frozen += 1
                audit.log_event("refinement", "section_frozen",
                                f"{sid} converged at iteration {n} (orchestrator + zero major)",
                                iteration=n, rubric_version=rubric.version)
            emit("section_update", section_id=sid, iteration=n, version=n,
                 selected=decision.selected_base, converged=do_freeze,
                 keyword_coverage=decision.keyword_coverage)

            # 5. per-section score
            section_scores[sid] = SectionScore(
                section_id=sid, section_type=stype,
                keyword_coverage=decision.keyword_coverage,
                claude_quality=decision.claude_quality, gpt_quality=decision.gpt_quality,
                selected_writer=decision.selected_base,
                converged=sid in frozen, current_version=n)

            # loop memory: carry forward MINOR suggestions only (F-19) — majors must stay
            # raisable until resolved (freeze depends on zero majors).
            for it in (cd.items + gd.items):
                if it.severity == "minor" and it.suggestion not in mem.rejected_suggestions:
                    mem.rejected_suggestions.append(it.suggestion)
            proposed_additions.extend(decision.rubric_additions)

        # frozen sections still need a SectionScore row (critique fields None)
        for sid in nonstatic:
            if sid not in section_scores:
                frozen_text = _current_text(ctx, manifest, sid)
                section_scores[sid] = SectionScore(
                    section_id=sid, section_type=section_types[sid],
                    keyword_coverage=round(keyword_coverage(
                        frozen_text, rubric, source_text=_raw_source(ctx, sid, frozen_text)), 4),
                    claude_quality=None, gpt_quality=None, selected_writer=None,
                    converged=True, current_version=manifest[sid]["version"])

        # rubric additions: pooled, JD-validated, capped (D-04)
        rubric, _added = validate_rubric_additions(
            rubric, proposed_additions, jd, n, model=model,
            max_additions=max_rubric_additions, client=claude_client, audit=audit)

        mem.frozen_sections = sorted(frozen)

        # aggregate IterationScore
        active_quals = [_selected_quality_for(section_scores[sid]) for sid in active]
        active_quals = [q for q in active_quals if q is not None]
        agg_quality = round(sum(active_quals) / len(active_quals), 4) if active_quals else None
        agg_keyword = _aggregate_keyword_coverage(ctx, manifest, rubric)
        keyword_delta = round(agg_keyword - prev_keyword, 4)
        quality_delta = round(agg_quality - prev_quality, 4) \
            if (agg_quality is not None and prev_quality is not None) else 0.0
        remaining_active = len([sid for sid in nonstatic if sid not in frozen])

        iter_score = IterationScore(
            iteration=n, keyword_coverage=agg_keyword, critique_score=agg_quality,
            keyword_delta=keyword_delta, quality_delta=quality_delta,
            sections_converged=newly_frozen, sections_active=remaining_active,
            section_scores=section_scores)
        iterations.append(iter_score)
        ctx.write_checkpoint(f"iteration_{n}", iter_score)
        ctx.write_checkpoint(f"loop_memory_{n}", mem.to_dict())
        emit("iteration_complete", iteration=n, keyword_coverage=agg_keyword,
             quality=agg_quality, keyword_delta=keyword_delta, quality_delta=quality_delta,
             frozen=newly_frozen, active=remaining_active)
        audit.log_event("refinement", "iteration_scored",
                        f"iter {n}: coverage {agg_keyword}, quality {agg_quality}, "
                        f"Δkw {keyword_delta}, Δq {quality_delta}, "
                        f"{newly_frozen} frozen, {remaining_active} active",
                        iteration=n, keyword_score=agg_keyword, critique_score=agg_quality,
                        rubric_version=rubric.version)

        # termination (D-05). Most informative reason first; never exceed max.
        zero_major = major_total == 0
        if remaining_active == 0:
            converged, reason = True, "all_sections_converged"
        elif n >= 2 and abs(keyword_delta) < keyword_delta_threshold \
                and abs(quality_delta) < critique_delta_threshold:
            converged, reason = True, "dual_signal_converged"
        elif zero_major:
            converged, reason = True, "soft_stop_zero_major"
        elif n >= max_iterations:
            converged, reason = False, "max_iterations"

        prev_keyword, prev_quality = agg_keyword, agg_quality
        if reason:
            break

    # unresolved = open items on sections that never converged (Phase 4 HITL), deduped by issue
    unresolved: dict[str, list] = {}
    for sid in nonstatic:
        if sid in frozen:
            continue
        seen, deduped = set(), []
        for it in latest_items.get(sid, []):
            if it.issue not in seen:
                seen.add(it.issue)
                deduped.append(it)
        if deduped:
            unresolved[sid] = deduped

    audit.log_event("refinement", "loop_end", f"converged={converged} ({reason})",
                    rubric_version=rubric.version)
    return RefinementResult(iterations=iterations, final_rubric=rubric, converged=converged,
                            convergence_reason=reason, manifest=manifest, memory=mem,
                            unresolved=unresolved)


def _selected_quality_for(s: SectionScore) -> float | None:
    """Quality of the selected draft for an aggregate — mirrors _selected_quality on
    a stored SectionScore."""
    if s.selected_writer == "claude":
        return s.claude_quality
    if s.selected_writer == "gpt":
        return s.gpt_quality
    if s.selected_writer == "synthesis" and s.claude_quality is not None and s.gpt_quality is not None:
        return max(s.claude_quality, s.gpt_quality)
    return None
