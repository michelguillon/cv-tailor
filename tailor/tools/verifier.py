"""tools/verifier.py — the fabrication gate (F-35).

A final, source-grounded truth check: for each tailored section, compare the selected
draft against the RAW corpus text and flag any claim the source doesn't support — a new
metric, title, employer, date, industry/sector/domain, named capability, or a JD keyword
asserted without evidence. This is the trust mechanism: nothing ships unflagged that the
candidate's own CV doesn't already say. Flags surface in the Phase-4 review (the human
gate) and the Phase-6 report (provenance), and are logged to the audit trail.

`verify_section` is the LLM-as-tool (Haiku, hidden behind helpers like every tool, D-02);
`verify_run` applies it across a run's non-static sections and returns flags as
`CritiqueItem`s so they flow through the existing review + report machinery unchanged.
Verification is a safety net: a failed check never crashes the run (returns no findings).
"""

from __future__ import annotations

from tailor.helpers import claude_complete
from tailor.models import CritiqueItem

__all__ = ["verify_section", "verify_run", "VERIFIER"]

VERIFIER = "verifier"

_TOOL = {
    "name": "report_grounding",
    "description": "Report any claim in the DRAFT that the SOURCE does not support (fabrication).",
    "input_schema": {
        "type": "object",
        "properties": {
            "unsupported": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "the exact span in the draft the source does not support"},
                        "kind": {"type": "string", "description": "metric | title | employer | date | sector | capability | keyword | other"},
                        "reason": {"type": "string", "description": "why the source does not substantiate it"},
                    },
                    "required": ["claim", "kind", "reason"],
                },
            },
        },
        "required": ["unsupported"],
    },
}

_SYSTEM = """\
You are a precise CV fact-checker. You are given the candidate's ORIGINAL source text for \
ONE section and a tailored DRAFT of it. Report ONLY draft statements that introduce a NEW, \
CHECKABLE FACT the source does not support: a specific metric/number, an employer or client \
name, a job title or seniority, an industry/sector/domain, a date, or a named \
system/product/capability.

CRITICAL — the following are NOT fabrication. NEVER flag them:
- rewording, paraphrasing, summarising, condensing, reordering, or dropping detail;
- generic phrasing, adjectives, or framing ("strategic", "scalable", "solution-led", \
"combines engineering foundations", "structured engagement");
- a fact that IS in the source but worded differently or with a qualifier changed/dropped — \
e.g. source "$25M book of business with Tier 1 agencies" vs draft "$25M business with \
enterprise clients" is SUPPORTED; "15-person org at Xandr" vs "15-person org" is SUPPORTED.

Method: for each candidate fact in the draft, FIND the supporting span in the source. If any \
source sentence conveys that fact (even loosely or in other words), it is SUPPORTED — do not \
flag it. Flag ONLY a concrete fact with NO basis anywhere in the source (e.g. a sector the \
source never names like "fintech"/"payments" for adtech work, an invented metric, or a \
client/title that doesn't appear). When unsure, do NOT flag. Quote the exact draft span and \
say which source fact is missing. If nothing qualifies, return an empty list. Call \
report_grounding exactly once."""


def verify_section(draft_text: str, source_text: str, *, model: str, client=None) -> list[dict]:
    """Return [{claim, kind, reason}] for content in `draft_text` not supported by
    `source_text`; [] when fully grounded. Retried once, then treated as no-finding
    (the gate is a safety net, never a hard pipeline failure — R-09 surfaces, doesn't crash)."""
    user = (f"SOURCE (ground truth):\n{source_text}\n\n"
            f"--- DRAFT ---\n{draft_text}\n\n"
            f"List every claim in the draft the source does not support.")
    for _ in range(2):
        resp = claude_complete(
            model=model, system=_SYSTEM, messages=[{"role": "user", "content": user}],
            tools=[_TOOL], tool_choice={"type": "tool", "name": "report_grounding"},
            max_tokens=700, client=client)
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "report_grounding":
                items = block.input.get("unsupported")
                if isinstance(items, list):
                    return [{"claim": str(i.get("claim", "")).strip(),
                             "kind": str(i.get("kind", "other")).strip(),
                             "reason": str(i.get("reason", "")).strip()}
                            for i in items if str(i.get("claim", "")).strip()]
    return []


def verify_run(ctx, manifest: dict, *, model: str, client=None) -> dict[str, list[CritiqueItem]]:
    """Verify every non-static section's final text against its raw corpus source.
    Returns {section_id: [CritiqueItem(severity='major', source_writer='verifier'), ...]}
    for sections with unsupported claims, so they flow through Phase-4 review + the report."""
    flags: dict[str, list[CritiqueItem]] = {}
    for sid, m in manifest.items():
        if m["static"] or not ctx.has_section(sid, source=True):
            continue                                  # static = verbatim; no source = nothing to check
        draft = ctx.read_section(sid, version=m["version"])
        source = ctx.read_section(sid, source=True)
        found = verify_section(draft, source, model=model, client=client)
        if found:
            flags[sid] = [
                CritiqueItem(
                    section=sid, severity="major",
                    issue=f'Unsupported claim (not in your CV): "{f["claim"]}"',
                    suggestion=f"Remove or rephrase to match the source — {f['kind']}: {f['reason']}",
                    source_writer=VERIFIER)
                for f in found
            ]
    return flags
