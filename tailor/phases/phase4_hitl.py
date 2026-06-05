"""Phase 4 — Human review (HITL). SPEC §5 Phase 4, D-18. Step 7.

Input:  RefinementResult (iterations, manifest, unresolved items), JDAnalysis,
        ScoringRubric, budgets, RunContext.
Output: human-confirmed or revised section files.

Preview-before-apply (D-18): the terminal display summarises section status and
lists unresolved items as lettered options plus a free-text escape hatch [e].
Free text is interpreted by Haiku into a structured {section_id, instruction}
decision, shown back to the human, and only then executed — by a single Claude
writer pass with the instruction as its direction (reusing tools/claude_writer.py,
not a new tool). This module provides render + interpret + revise; the actual
terminal input loop lives in run.py (Step 8), like Phase 1's render_fit_hitl.
"""

from __future__ import annotations

from tailor.helpers import claude_complete
from tailor.tools import claude_writer

__all__ = ["render_section_review", "unresolved_list", "interpret_freetext",
           "revise_section", "converged_at", "HITLError"]


class HITLError(RuntimeError):
    pass


def converged_at(result, sid: str) -> int | None:
    """The iteration a section converged at, or None if it never did. Reused by the
    terminal review render and the Web UI's checkpoint payload (§12.3)."""
    for it in result.iterations:
        s = it.section_scores.get(sid)
        if s is not None and s.converged:
            return it.iteration
    return None


_converged_at = converged_at   # backward-compatible alias (used below)


def unresolved_list(result) -> list[tuple[str, object]]:
    """Flatten unresolved items to a numbered list: [(section_id, CritiqueItem), ...]."""
    out = []
    for sid, items in result.unresolved.items():
        for it in items:
            out.append((sid, it))
    return out


def render_section_review(result, *, max_iterations: int | None = None) -> str:
    """Terminal HITL display: progression, per-section status, unresolved items."""
    its = result.iterations
    cov = " → ".join(f"{it.keyword_coverage:.0%}" for it in its) or "—"
    qual = " → ".join("—" if it.critique_score is None else f"{it.critique_score:.1f}" for it in its) or "—"
    n = len(its)
    cap = f"{n} / {max_iterations}" if max_iterations else str(n)

    lines = [
        "─── Refinement complete ───────────────────────────────────",
        f"  Iterations:       {cap}  ({result.convergence_reason})",
        f"  Keyword coverage: {cov}",
        f"  Quality (sel.):   {qual}",
        "",
        "  Section status:",
    ]
    manifest = result.manifest
    for sid in sorted(manifest):
        m = manifest[sid]
        disp = m.get("label") or m.get("title") or sid
        label = disp if len(disp) <= 34 else disp[:31] + "…"
        if m["static"]:
            lines.append(f"  — {label:36} static")
            continue
        citer = _converged_at(result, sid)
        ver = f"v{m['version']}"
        if citer is not None:
            lines.append(f"  ✓ {label:36} converged iter {citer}  ({ver})")
        else:
            lines.append(f"  ~ {label:36} active            ({ver})  ← did not converge")

    items = unresolved_list(result)
    if items:
        lines += ["", f"  Unresolved items ({len(items)}):"]
        for i, (sid, it) in enumerate(items, 1):
            disp = manifest[sid].get("label") or manifest[sid].get("title") or sid
            lines.append(f"  [{i}] {disp}: \"{it.issue}\" ({it.severity})")
        lines += [
            "",
            "  Options:",
            "  [a] Accept all and proceed",
            "  [b..] Apply unresolved item by number (e.g. b1)",
            "  [d] Leave all unresolved and proceed",
            "  [e] Something else — describe what you want",
        ]
    else:
        lines += ["", "  No unresolved items.  Options: [a] accept and proceed  [e] revise something"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Free-text interpretation (Haiku) → structured decision (D-18)                #
# --------------------------------------------------------------------------- #

_INTERPRET_TOOL = {
    "name": "interpret_revision",
    "description": "Map the human's free-text request to one section and a concrete revision instruction.",
    "input_schema": {
        "type": "object",
        "properties": {
            "section_id": {"type": "string", "description": "the section to revise (must be one of the listed ids)"},
            "instruction": {"type": "string", "description": "a concrete, self-contained revision instruction"},
        },
        "required": ["section_id", "instruction"],
    },
}


def interpret_freetext(text: str, result, *, model: str, client=None) -> dict:
    """Haiku interprets free text into {section_id, instruction}. Validated against
    the manifest's non-static sections; retried once (R-09). Shown to the human
    before execution (preview-before-apply)."""
    editable = {sid: result.manifest[sid].get("label") or result.manifest[sid].get("title") or sid
                for sid in result.manifest if not result.manifest[sid]["static"]}
    listing = "\n".join(f"  - {sid} ({title})" for sid, title in editable.items())
    prompt = (
        f"The human reviewing a tailored CV said:\n\"{text}\"\n\n"
        f"Editable sections (use the exact id):\n{listing}\n\n"
        f"Pick the section they mean and turn their request into a concrete instruction."
    )
    data = None
    for _ in range(2):
        resp = claude_complete(
            model=model, system="You map a human's CV-revision request to one section and a clear instruction.",
            messages=[{"role": "user", "content": prompt}],
            tools=[_INTERPRET_TOOL], tool_choice={"type": "tool", "name": "interpret_revision"},
            max_tokens=300, client=client,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "interpret_revision":
                data = block.input
                break
        if data and data.get("section_id") in editable and str(data.get("instruction", "")).strip():
            return {"section_id": data["section_id"], "instruction": data["instruction"].strip()}
        data = None
    raise HITLError("could not interpret the request into a known section + instruction")


def revise_section(section_id: str, instruction: str, result, jd, rubric, budgets, ctx, *,
                   model: str, client=None) -> tuple[int, str]:
    """Apply a human-directed revision via one Claude writer pass. Writes the next
    version, updates the manifest, returns (version, text)."""
    manifest = result.manifest
    m = manifest.get(section_id)
    if m is None or m["static"]:
        raise HITLError(f"{section_id} is not an editable section")
    current = ctx.read_section(section_id, version=m["version"])
    new_version = m["version"] + 1
    draft = claude_writer.write_section(
        section_id, current, jd, rubric, budgets.get(m["section_type"]),
        version=new_version, direction=instruction, is_final=True, model=model, client=client)
    ctx.write_section(section_id, draft.text, version=new_version)
    m["version"] = new_version
    m["word_count"] = len(draft.text.split())
    ctx.audit.log_event("phase4_hitl", "human_revision",
                        f"{section_id} → v{new_version} (instruction: {instruction})")
    return new_version, draft.text
