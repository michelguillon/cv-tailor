# CLAUDE.md — tailor/phases/

The pipeline phases (SPEC §5). Deterministic, fixed order — **except**
`phase3_refinement.py`, the one agentic region (D-01). Read the package
`tailor/CLAUDE.md` first.

## Conventions across all phases

- **A phase reads the previous phase's checkpoint and writes its own** before
  returning (R-06). `phase3` is fully checkpoint-driven: its input is the Phase 2
  manifest (`section_id → {static, version, source_cv, path, section_type}`), so
  it never re-reads the corpus. If a phase needs a new field downstream, add it to
  the producing phase's manifest/checkpoint, not a side channel.
- **Structured LLM output is forced and validated.** Use a forced tool call
  (Anthropic `tool_choice`) or strict `response_format` (OpenAI), validate against
  the schema, **retry once, then surface** — never let partial output flow
  downstream (R-09). On a hard failure, prefer leaving state unchanged over
  corrupting it (see `tools/rubric.py`).
- **The drafter/reviser must never fabricate.** Phases 2 and 3 change emphasis,
  ordering, and wording only — never invent employers, titles, dates, or metrics.
  This rule is stated in the prompt *and* relied on in review.
- **HITL is preview-before-apply** (Phases 1, 4, 5): show what changes, then ask.

## phase3_refinement.py — the agentic loop (D-01, D-05, D-12)

- **Section is the unit of work.** Drafting, critique, scoring, freezing, and
  convergence are all per-section. Static sections never enter the loop (D-13).
- **The orchestrator (Claude) accepts/rejects each `CritiqueItem`** and logs its
  reasoning (D-06). Set `accepted_by_orchestrator`; set `applied` when the
  acceptance is reflected in a new version file. accepted-but-not-applied is an
  anomaly — log it (D-07 #1).
- **Freeze a section when its critique has zero `major` items** → `converged=True`,
  excluded from later critique calls. This is what makes iteration 2+ cheaper.
- **Termination is dual-signal** (D-05): `abs(keyword_delta) < 0.05` **AND**
  `abs(critique_delta) < 0.5`. Other exits: all sections frozen; soft-stop when the
  iteration had zero `major` items; `max_iterations` as the hard ceiling. The loop
  reports the most informative reason but never exceeds the cap. Don't add a
  termination path without recording it (and confirm thresholds against real
  deltas — F-16).
- **Aggregate `keyword_coverage` is UNION coverage** across non-static sections
  (F-15), not a mean. Aggregate `critique_score` is the mean over *active*
  sections, so it can dip as easy sections freeze — expected, absorbed by the 0.5
  threshold (F-16).
