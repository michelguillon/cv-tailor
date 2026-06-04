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

Aggregate keyword_coverage is UNION coverage across non-static sections (F-15);
aggregate quality is the mean of the SELECTED draft's quality across active
sections, so it can dip as easy sections freeze — absorbed by the 0.5 threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tailor.models import IterationScore, ScoringRubric, SectionScore
from tailor.tools import claude_writer, gpt_writer, orchestrator_tool
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


def _aggregate_keyword_coverage(ctx, manifest, rubric) -> float:
    texts = [_current_text(ctx, manifest, sid) for sid, m in manifest.items() if not m["static"]]
    return round(union_coverage(texts, rubric), 4) if texts else 0.0


def _write_writer_draft(ctx, draft):
    """Per-writer draft on disk for the Changes tab / inspection (not load-bearing)."""
    path = ctx.sections_dir / f"{draft.section_id}_{draft.writer}_v{draft.version}.md"
    path.write_text(draft.text.rstrip() + "\n", encoding="utf-8")


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
) -> RefinementResult:
    """Run the dual-writer refinement loop. See the module docstring for the flow."""
    audit = ctx.audit
    section_types = {sid: m["section_type"] for sid, m in manifest.items()}
    nonstatic = [sid for sid, m in manifest.items() if not m["static"]]
    frozen: dict[str, int] = {}
    mem = LoopMemory()

    iterations: list[IterationScore] = []
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

            # 1. dual write
            cd = claude_writer.write_section(
                sid, current, jd, rubric, budget, version=n, direction=direction,
                rejected_suggestions=mem.rejected_suggestions, is_final=is_final,
                model=model, client=claude_client)
            gd = gpt_writer.write_section(
                sid, current, jd, rubric, budget, version=n, direction=direction,
                rejected_suggestions=mem.rejected_suggestions, is_final=is_final,
                model=gpt_model, client=openai_client)
            _write_writer_draft(ctx, cd)
            _write_writer_draft(ctx, gd)

            # 2. adjudicate → write selected text as v(n)
            prior = section_scores.get(sid)
            decision, selected_text = orchestrator_tool.adjudicate(
                sid, cd, gd, rubric, jd, prior_score=prior, is_final=is_final,
                model=model, client=claude_client)
            ctx.write_section(sid, selected_text, version=n)
            manifest[sid]["version"] = n
            manifest[sid]["word_count"] = len(selected_text.split())
            audit.log_event("refinement", "section_adjudicated",
                            f"{sid} → v{n}: base={decision.selected_base} "
                            f"(claude {decision.claude_quality}/gpt {decision.gpt_quality}); "
                            f"direction: {decision.direction}",
                            iteration=n, keyword_score=decision.keyword_coverage,
                            critique_score=_selected_quality(decision), rubric_version=rubric.version)

            # 3. pushback (one exchange; skip on the final pass)
            new_direction = decision.direction
            if not is_final:
                cp = claude_writer.pushback(sid, decision, cd, jd, model=model, client=claude_client)
                gp = gpt_writer.pushback(sid, decision, gd, jd, model=gpt_model, client=openai_client)
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
            majors = [it for it in (cd.items + gd.items) if it.severity == "major"]
            major_total += len(majors)
            do_freeze = decision.converged and not majors
            if do_freeze:
                frozen[sid] = n
                newly_frozen += 1
                audit.log_event("refinement", "section_frozen",
                                f"{sid} converged at iteration {n} (orchestrator + zero major)",
                                iteration=n, rubric_version=rubric.version)

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
                section_scores[sid] = SectionScore(
                    section_id=sid, section_type=section_types[sid],
                    keyword_coverage=round(keyword_coverage(_current_text(ctx, manifest, sid), rubric), 4),
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

    audit.log_event("refinement", "loop_end", f"converged={converged} ({reason})",
                    rubric_version=rubric.version)
    return RefinementResult(iterations=iterations, final_rubric=rubric, converged=converged,
                            convergence_reason=reason, manifest=manifest, memory=mem)


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
