"""Phase 3 — Refinement loop (the one agentic region, D-01). SPEC §5 Phase 3, Step 6.

Input:  the Phase 2 manifest (section_id → {static, version, source_cv, path,
        section_type}), JDAnalysis, ScoringRubric, budgets, RunContext.
Output: versioned section files (<id>_v(n+1).md), an updated ScoringRubric, a
        list[IterationScore], and per-iteration checkpoints. Reasoning logged to
        run_log.jsonl (D-06), never fed back into context.
Models: Claude orchestrator (Haiku/dev, Sonnet/full) + GPT-4o-mini critique tool.

Section is the unit of work (D-12). Each iteration, per ACTIVE (non-frozen,
non-static) section:

  1. critique active sections (GPT-4o-mini, tools/critique.py) — length-budget
     items (D-14) arrive as CritiqueItems already.
  2. the orchestrator (Claude) accepts/rejects EACH CritiqueItem, logging its
     reasoning (D-06). This is the genuine judgment region — accept a suggestion
     that improves fit/truthfulness, reject one that would harm (e.g. drop a
     quantified achievement the JD wants, or invent experience).
  3. revise each section with ≥1 accepted item → write v(n+1); mark those items
     applied. accepted-but-not-applied is an anomaly (D-07 #1), logged as such.
  4. validate critique's rubric_additions against the JD (max 2/iter, version++,
     D-04 — tools/rubric.py).
  5. freeze every active section with zero MAJOR items this iteration
     (converged=True) — frozen sections are excluded from later critique calls,
     which is what makes iteration 2+ cheaper and more focused.
  6. build per-section + aggregate IterationScore with deltas; checkpoint (R-06).
  7. terminate on dual-signal convergence (kw_delta AND crit_delta below
     threshold), all-frozen, soft-stop (zero major items), or max_iterations
     (D-05). max_iterations is the hard ceiling; the others report a more
     informative reason when they fire at/under the cap.

Aggregate keyword_coverage is UNION coverage across non-static sections (F-15):
the schema field is loosely labelled "weighted mean", but the SPEC §Phase-4
example progression (61%→74%→83%) and the CV-level metric established in F-11 are
union coverage. A mean of per-section coverages sits far lower and would not feed
the 0.05 delta threshold meaningfully.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tailor.helpers import claude_complete
from tailor.models import IterationScore, ScoringRubric, SectionScore
from tailor.tools.critique import critique_sections
from tailor.tools.rubric import validate_rubric_additions
from tailor.tools.scorer import keyword_coverage, union_coverage

__all__ = ["refine", "RefinementResult", "RefinementError"]


class RefinementError(RuntimeError):
    pass


@dataclass
class RefinementResult:
    iterations: list[IterationScore]
    final_rubric: ScoringRubric
    converged: bool
    convergence_reason: str
    manifest: dict           # mutated in place: section versions advanced


# --------------------------------------------------------------------------- #
# Orchestrator: accept / reject each critique item (the agentic core, D-06)   #
# --------------------------------------------------------------------------- #

_DECISION_TOOL = {
    "name": "submit_decisions",
    "description": "Accept or reject each numbered critique item for this CV revision.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "accept": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "accept", "reason"],
                },
            },
        },
        "required": ["decisions"],
    },
}

_DECISION_SYSTEM = """\
You are the orchestrator deciding which reviewer critiques to act on for a CV \
being tailored to a specific role. For EACH numbered item, accept it if applying \
the suggestion genuinely strengthens the application for THIS role and stays \
truthful; reject it if it would weaken the CV, remove evidence the JD wants \
(e.g. a quantified achievement), introduce fabrication, or chase a requirement \
the role doesn't need. Your reasoning is recorded in the audit trail, so be \
specific. Call submit_decisions exactly once, one decision per item index."""

_REVISE_SYSTEM = """\
You revise ONE CV section by applying the reviewer's accepted suggestions, \
truthfully. Apply only what the suggestions ask; otherwise preserve the section.
HARD RULES:
- Do NOT invent or alter employers, job titles, dates, companies, metrics, or \
facts. Change emphasis, ordering, and wording only.
- Keep every claim traceable to the existing section. If a suggestion can't be \
satisfied without fabricating, ignore that suggestion.
- Preserve structure (bullets stay bullets) and stay close to ~{target} words.
- Output ONLY the revised section text (markdown), no heading, no commentary."""


def _decision_prompt(items, jd) -> str:
    lines = []
    for i, it in enumerate(items):
        lines.append(f"[{i}] ({it.section} / {it.severity}) {it.issue}  →  {it.suggestion}")
    return (
        f"ROLE: {jd.role_title} ({jd.seniority_level})\n"
        f"JD KEY REQUIREMENTS:\n" + "\n".join(f"  - {r}" for r in jd.key_requirements) + "\n\n"
        f"CRITIQUE ITEMS:\n" + "\n".join(lines) + "\n\n"
        f"Decide accept/reject for each item index."
    )


def _adjudicate(items, jd, *, model, client) -> dict[int, tuple[bool, str]]:
    """Orchestrator accept/reject per item. Returns {index: (accept, reason)}.

    Missing decisions default to reject ("no decision returned") so an item is
    never silently applied without an orchestrator call behind it.
    """
    if not items:
        return {}
    prompt = _decision_prompt(items, jd)
    decisions = None
    for _ in range(2):
        resp = claude_complete(
            model=model,
            system=_DECISION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_DECISION_TOOL],
            tool_choice={"type": "tool", "name": "submit_decisions"},
            max_tokens=1500,
            client=client,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_decisions":
                decisions = block.input.get("decisions", [])
                break
        if decisions is not None:
            break
    if decisions is None:
        raise RefinementError(f"orchestrator did not return critique decisions ({model})")

    out: dict[int, tuple[bool, str]] = {}
    for d in decisions:
        idx = d.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            out[idx] = (bool(d.get("accept")), str(d.get("reason", "")))
    for i in range(len(items)):
        out.setdefault(i, (False, "no decision returned"))
    return out


def _revise_section(section_id, section_type, current_text, accepted_items, jd, budget, *, model, client) -> str:
    source_wc = len(current_text.split())
    target = min(max(source_wc, budget.min_words), budget.max_words) if budget else (source_wc or 120)
    asks = "\n".join(f"  - {it.issue}  →  {it.suggestion}" for it in accepted_items)
    user = (
        f"ROLE: {jd.role_title}\n"
        f"SECTION TYPE: {section_type}\n"
        f"TARGET WORDS: ~{target}\n\n"
        f"ACCEPTED SUGGESTIONS TO APPLY:\n{asks}\n\n"
        f"CURRENT SECTION:\n{current_text}"
    )
    resp = claude_complete(
        model=model,
        system=_REVISE_SYSTEM.format(target=target),
        messages=[{"role": "user", "content": user}],
        max_tokens=max(256, target * 6),
        temperature=0.0,
        client=client,
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise RefinementError(f"empty revision for {section_id}")
    return text


# --------------------------------------------------------------------------- #
# Scoring helpers                                                             #
# --------------------------------------------------------------------------- #

def _current_text(ctx, manifest, sid) -> str:
    m = manifest[sid]
    if m["static"]:
        return ctx.read_section(sid, static=True)
    return ctx.read_section(sid, version=m["version"])


def _aggregate_keyword_coverage(ctx, manifest, rubric) -> float:
    """Union coverage across all non-static sections (F-15) — the CV-level metric."""
    texts = [_current_text(ctx, manifest, sid) for sid, m in manifest.items() if not m["static"]]
    return round(union_coverage(texts, rubric), 4) if texts else 0.0


# --------------------------------------------------------------------------- #
# The loop                                                                    #
# --------------------------------------------------------------------------- #

def refine(
    manifest: dict,
    jd,
    rubric: ScoringRubric,
    budgets: dict,
    ctx,
    *,
    model: str,
    critique_model: str = "gpt-4o-mini",
    max_iterations: int = 1,
    keyword_delta_threshold: float = 0.05,
    critique_delta_threshold: float = 0.5,
    max_rubric_additions: int = 2,
    claude_client=None,
    openai_client=None,
) -> RefinementResult:
    """Run the section-granular refinement loop. See module docstring for the flow."""
    audit = ctx.audit
    section_types = {sid: m["section_type"] for sid, m in manifest.items()}
    nonstatic = [sid for sid, m in manifest.items() if not m["static"]]
    frozen: dict[str, int] = {}          # section_id → iteration it converged in

    iterations: list[IterationScore] = []
    prev_keyword = _aggregate_keyword_coverage(ctx, manifest, rubric)   # baseline (v0 drafts)
    prev_critique: float | None = None
    converged, reason = False, ""

    audit.log_event("refinement", "loop_start",
                    f"{len(nonstatic)} active sections; max_iterations={max_iterations}",
                    keyword_score=prev_keyword, rubric_version=rubric.version)

    for n in range(1, max_iterations + 1):
        active = [sid for sid in nonstatic if sid not in frozen]
        if not active:
            converged, reason = True, "all_sections_converged"
            break

        # 1. critique only the active sections (frozen ones are excluded → cheaper)
        active_text = {sid: _current_text(ctx, manifest, sid) for sid in active}
        critique = critique_sections(
            active_text, jd, rubric,
            model=critique_model, budgets=budgets,
            section_types={sid: section_types[sid] for sid in active},
            client=openai_client,
        )
        ctx.write_checkpoint(f"critique_iter_{n}", critique)

        # 2. orchestrator accepts/rejects each item (the agentic core, D-06)
        decisions = _adjudicate(critique.items, jd, model=model, client=claude_client)
        accepted_by_section: dict[str, list] = defaultdict(list)
        for i, it in enumerate(critique.items):
            accept, why = decisions[i]
            it.accepted_by_orchestrator = accept
            if accept:
                accepted_by_section[it.section].append(it)
                audit.log_event("refinement", "critique_item_accepted",
                                f"{it.section} [{it.severity}]: {it.issue} — {why}",
                                iteration=n, rubric_version=rubric.version)
            else:
                it.rejection_reason = why
                audit.log_event("refinement", "critique_item_rejected",
                                f"{it.section} [{it.severity}]: {it.issue} — {why}",
                                iteration=n, rubric_version=rubric.version)

        # 3. revise each section with accepted items → write v(n+1)
        for sid, items in accepted_by_section.items():
            new_version = manifest[sid]["version"] + 1
            new_text = _revise_section(
                sid, section_types[sid], active_text[sid], items, jd,
                budgets.get(section_types[sid]), model=model, client=claude_client,
            )
            ctx.write_section(sid, new_text, version=new_version)
            manifest[sid]["version"] = new_version
            manifest[sid]["word_count"] = len(new_text.split())
            for it in items:
                it.applied = True       # acceptance reflected in the new draft
            audit.log_event("refinement", "section_revised",
                            f"{sid} → v{new_version} ({len(items)} suggestion(s) applied)",
                            iteration=n, rubric_version=rubric.version)

        # accepted-but-not-applied is an anomaly (D-07 #1) — nothing should reach here,
        # but assert it explicitly so a future bug surfaces in the audit trail.
        for it in critique.items:
            if it.accepted_by_orchestrator and not it.applied:
                audit.log_event("refinement", "anomaly_accepted_not_applied",
                                f"{it.section}: accepted but not reflected in a revision — {it.issue}",
                                iteration=n, rubric_version=rubric.version)

        # 4. validate critique-surfaced rubric additions against the JD (D-04)
        rubric, added = validate_rubric_additions(
            rubric, critique.rubric_additions, jd, n,
            model=model, max_additions=max_rubric_additions,
            client=claude_client, audit=audit,
        )

        # 5. freeze sections with zero MAJOR items this iteration (D-12)
        major_by_section: dict[str, int] = defaultdict(int)
        for it in critique.items:
            if it.severity == "major":
                major_by_section[it.section] += 1
        newly_frozen = 0
        for sid in active:
            if major_by_section[sid] == 0:
                frozen[sid] = n
                newly_frozen += 1
                audit.log_event("refinement", "section_frozen",
                                f"{sid} converged (zero major items) at iteration {n}",
                                iteration=n, rubric_version=rubric.version)

        # 6. per-section + aggregate IterationScore
        section_scores: dict[str, SectionScore] = {}
        for sid in nonstatic:
            text = _current_text(ctx, manifest, sid)
            was_active = sid in active
            section_scores[sid] = SectionScore(
                section_id=sid,
                section_type=section_types[sid],
                keyword_coverage=round(keyword_coverage(text, rubric), 4),
                # critique score only meaningful for sections critiqued this iter
                critique_score=critique.section_scores.get(sid) if was_active else None,
                converged=sid in frozen,
                current_version=manifest[sid]["version"],
            )

        active_scores = [s for sid, s in section_scores.items()
                         if sid in active and s.critique_score is not None]
        agg_critique = round(sum(s.critique_score for s in active_scores) / len(active_scores), 4) \
            if active_scores else None
        agg_keyword = _aggregate_keyword_coverage(ctx, manifest, rubric)
        keyword_delta = round(agg_keyword - prev_keyword, 4)
        critique_delta = round(agg_critique - prev_critique, 4) \
            if (agg_critique is not None and prev_critique is not None) else 0.0
        remaining_active = len([sid for sid in nonstatic if sid not in frozen])

        iter_score = IterationScore(
            iteration=n,
            keyword_coverage=agg_keyword,
            critique_score=agg_critique,
            keyword_delta=keyword_delta,
            critique_delta=critique_delta,
            sections_converged=newly_frozen,
            sections_active=remaining_active,
            section_scores=section_scores,
        )
        iterations.append(iter_score)
        ctx.write_checkpoint(f"iteration_{n}", iter_score)
        audit.log_event("refinement", "iteration_scored",
                        f"iter {n}: coverage {agg_keyword}, critique {agg_critique}, "
                        f"Δkw {keyword_delta}, Δcrit {critique_delta}, "
                        f"{newly_frozen} frozen, {remaining_active} active",
                        iteration=n, keyword_score=agg_keyword, critique_score=agg_critique,
                        rubric_version=rubric.version)

        # 7. termination (D-05). Report the most informative reason; never exceed max.
        zero_major = sum(major_by_section.values()) == 0
        if remaining_active == 0:
            converged, reason = True, "all_sections_converged"
        elif n >= 2 and abs(keyword_delta) < keyword_delta_threshold \
                and abs(critique_delta) < critique_delta_threshold:
            converged, reason = True, "dual_signal_converged"
        elif zero_major:
            converged, reason = True, "soft_stop_zero_major"
        elif n >= max_iterations:
            converged, reason = False, "max_iterations"

        prev_keyword, prev_critique = agg_keyword, agg_critique
        if reason:
            break

    audit.log_event("refinement", "loop_end", f"converged={converged} ({reason})",
                    rubric_version=rubric.version)
    return RefinementResult(
        iterations=iterations, final_rubric=rubric,
        converged=converged, convergence_reason=reason, manifest=manifest,
    )
