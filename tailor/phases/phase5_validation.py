"""Phase 5 — Formatting validation (Claude Haiku). SPEC §5 Phase 5. Step 7.

Input:  human-confirmed section files, budgets, RunContext.
Output: per-section formatting corrections + an assembled-length check.

A cheap Haiku pass normalises each NON-STATIC section (punctuation, em/en-dash,
bullet parallelism, tense, date format) — static sections are the person's own
verbatim text and stay untouched (D-13). The HITL here is yes/no only (D-18): the
corrections are shown as a diff and applied as-is or not at all. The terminal
confirm lives in run.py (Step 8); this module provides validate / render / apply
and the assembled-length check.
"""

from __future__ import annotations

from tailor.helpers import claude_complete

__all__ = ["validate_formatting", "assembled_length_check", "render_corrections",
           "apply_corrections"]


_FORMAT_TOOL = {
    "name": "submit_formatting",
    "description": "Return the section with formatting normalised, and the list of corrections made.",
    "input_schema": {
        "type": "object",
        "properties": {
            "corrected_text": {"type": "string"},
            "corrections": {"type": "array", "items": {"type": "string"},
                            "description": "one short line per correction; empty if nothing to fix"},
        },
        "required": ["corrected_text", "corrections"],
    },
}

_SYSTEM = """\
You are a CV formatting checker. Fix ONLY mechanical consistency, never content or \
wording: punctuation consistency, em-dash/en-dash/hyphen usage, bullet-point \
parallelism, verb-tense consistency, and date-format consistency. Do NOT add, \
remove, or reword any factual claim, and do NOT change the meaning. If nothing \
needs fixing, return the text unchanged with an empty corrections list. Call \
submit_formatting exactly once."""


def _format_one(text: str, *, model: str, client=None) -> tuple[str, list[str]]:
    for _ in range(2):
        resp = claude_complete(
            model=model, system=_SYSTEM,
            messages=[{"role": "user", "content": f"SECTION:\n{text}"}],
            tools=[_FORMAT_TOOL], tool_choice={"type": "tool", "name": "submit_formatting"},
            max_tokens=max(512, len(text.split()) * 8), client=client,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_formatting":
                corrected = block.input.get("corrected_text")
                if isinstance(corrected, str) and corrected.strip():
                    return corrected.strip(), [str(c) for c in block.input.get("corrections", [])]
    # Formatting is non-critical: on failure, leave the section unchanged.
    return text.strip(), []


def validate_formatting(ctx, manifest: dict, *, model: str, client=None) -> dict:
    """Per non-static section, propose formatting corrections. Returns only the
    sections that actually change: {sid: {original, corrected, corrections}}."""
    out: dict[str, dict] = {}
    for sid, m in manifest.items():
        if m["static"]:
            continue
        text = ctx.read_section(sid, version=m["version"])
        corrected, corrections = _format_one(text, model=model, client=client)
        if corrections and corrected.strip() != text.strip():
            out[sid] = {"original": text.strip(), "corrected": corrected, "corrections": corrections}
    return out


def assembled_length_check(manifest: dict, budgets: dict) -> dict:
    """Sum section word counts vs the corpus-derived envelope (sum of per-section
    max_words). Surfaces the longest sections if the assembled CV runs over."""
    total = sum(m["word_count"] for m in manifest.values())
    budget_total = sum(budgets[m["section_type"]].max_words
                       for m in manifest.values() if m["section_type"] in budgets)
    longest = sorted(((sid, m["word_count"]) for sid, m in manifest.items() if not m["static"]),
                     key=lambda x: -x[1])[:3]
    return {"total_words": total, "budget_words": budget_total,
            "over_budget": total > budget_total > 0, "longest": longest}


def render_corrections(corrections: dict, length: dict) -> str:
    """Yes/no diff display for the formatting + length check."""
    lines = ["─── Formatting validation ─────────────────────────────────"]
    if not corrections:
        lines.append("  No formatting corrections needed.")
    else:
        for sid, c in corrections.items():
            lines.append(f"  {sid}:")
            for fix in c["corrections"]:
                lines.append(f"    · {fix}")
    lines += ["",
              f"  Assembled length: {length['total_words']} / {length['budget_words']} words "
              + ("⚠ OVER" if length["over_budget"] else "ok")]
    if length["over_budget"] and length["longest"]:
        lines.append("  Longest sections: " + ", ".join(f"{s} ({w}w)" for s, w in length["longest"]))
    lines += ["", "  Apply formatting corrections?  [y] yes  [n] no"]
    return "\n".join(lines)


def apply_corrections(ctx, corrections: dict, manifest: dict) -> list[str]:
    """Write each corrected section as the next version; update the manifest."""
    applied = []
    for sid, c in corrections.items():
        m = manifest[sid]
        v = (m["version"] or 0) + 1
        ctx.write_section(sid, c["corrected"], version=v)
        m["version"] = v
        m["word_count"] = len(c["corrected"].split())
        applied.append(sid)
        ctx.audit.log_event("phase5_validation", "formatting_applied",
                            f"{sid} → v{v}: {', '.join(c['corrections'])}")
    return applied
