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
- **The drafter/reviser must never fabricate (F-34).** Phases 2 and 3 change
  emphasis, ordering, and wording only — never invent employers, titles, dates, or
  metrics, **never inject a role headline/tagline, never relabel a role's nature or
  claim a sector/domain not in the source, and never insert a JD/rubric keyword the
  source doesn't already evidence** (an unsupported keyword is fabrication, not
  coverage — the keyword list is a relevance guide, NOT a checklist). The rules live
  in `tools/writer_common.TRUTHFULNESS_RULES` (shared by both writers) and are
  *enforced* by the orchestrator, which sees the source and gates fabrication —
  fabrication caps a draft's score and blocks convergence (F-34). Why this matters:
  `keyword_coverage` is a scored target, so without these guards a fluent model games
  it by fabricating (Goodhart). **The metric itself is now source-grounded (F-38):**
  `keyword_coverage`/`union_coverage` count a keyword only where the candidate's raw
  source evidences it, so an inserted-but-unsupported keyword earns *zero* coverage —
  the optimisation target no longer rewards the very thing the rules forbid. Prompt
  rules + orchestrator gate + verifier + honest metric all point the same way.
- **HITL is preview-before-apply** (Phases 1, 4, 5): show what changes, then ask.

## phase3_refinement.py — the dual-writer agentic loop (D-28, D-01, D-05, D-12)

- **Section is the unit of work.** Writing, adjudication, scoring, freezing, and
  convergence are all per-section. Static sections never enter the loop (D-13).
- **Two writers + one orchestrator** (D-28): `claude_writer` and `gpt_writer` each
  draft every active section independently; `orchestrator_tool.adjudicate` scores
  both, selects/synthesises, and sets `direction`. Selected text → `<id>_v<n>.md`;
  per-writer drafts → `<id>_<writer>_v<n>.md` for the Changes tab.
- **Graceful degradation on a writer failure (F-39):** each writer call goes through
  `_safe_write` (→ draft or None on any exception, logged `writer_failed`). If exactly
  one writer fails, the loop degrades to the survivor — a stand-in (survivor text, empty
  items) keeps `adjudicate` scoring/directing, then the selected text is forced to the
  survivor *verbatim* (no synth reword), provenance set honestly, the failed writer's
  pushback skipped, and `writer_degraded` emitted. Both failing → `WriterError` (surface,
  never ship blank). A transient GPT timeout must NOT abort the run.
- **One pushback exchange** (D-29): both writers may object to the direction once;
  the orchestrator holds or revises it (skipped on the final pass). All reasoning
  is logged (D-06), never fed back into context.
- **Freeze** = orchestrator `converged` **AND** zero `major` items across both
  drafts (majors are the canonical freeze/soft-stop source, D-28). Frozen sections
  are excluded from all later iterations — what makes iteration 2+ cheap.
- **`LoopMemory` is structured state, not prose** (D-30): per-section `directions`
  carried forward, MINOR suggestions accumulated as `rejected_suggestions` (majors
  stay raisable until resolved — F-19), frozen set, score history. D-06 preserved.
- **Termination is dual-signal** (D-05): `abs(keyword_delta) < 0.05` **AND**
  `abs(quality_delta) < 0.5`. Other exits: all frozen; soft-stop when the iteration
  had zero `major` items; `max_iterations` as the hard ceiling (most informative
  reason wins, never exceeds the cap). Thresholds confirmed on real runs (F-16).
- **Aggregate `keyword_coverage` is SOURCE-GROUNDED UNION coverage** across
  non-static sections (F-15 + F-38): a keyword counts only where the raw corpus
  supports it (sources fall back to the draft when no Phase-2 source was persisted,
  preserving old behaviour for tests). Aggregate `critique_score` is the mean of the
  **selected** draft's quality over *active* sections, so it can dip as easy sections
  freeze — expected, absorbed by the 0.5 threshold (F-16).

## phase4_hitl / phase5_validation / phase6_output — review, format, emit (Step 7)

- **Phases provide render + logic; `run.py` owns the terminal `input()`** — same
  split as Phase 1's `render_fit_hitl` (no `stdin` in a phase → testable).
- **Phase 4 (HITL, D-18):** "unresolved" = the writers' self-assessed items on
  sections that never converged (`RefinementResult.unresolved`, deduped by issue).
  Free-text `[e]` → Haiku interprets to `{section_id, instruction}` (shown back
  first), then ONE `claude_writer` pass executes it (no new revise tool). Quality
  line = the **selected** draft's quality (F-24).
- **Phase 5:** Haiku formatting on **non-static** sections only (static stays
  verbatim, D-13); yes/no diff; accepted corrections write the next version.
  Assembled-length envelope = sum of per-section `max_words` (distinct from the
  per-section length check — both exist, F-25).
- **Phase 6:** checkpoint-driven (D-07 #3) — reads section files + the manifest,
  never the corpus. Order = (config `cv_sections` type index, then `position`); the
  manifest carries `position` + `title` from Phase 2 (F-23). Highest version per
  section (or static). Jinja `templates/output.html`, **6 tabs (Fit/CV/Grounding/
  Changes/Scores/Reasoning)**; word-level diffs via `difflib`. `cv_final.md` is the
  clean artefact. The **Fit tab** (F-39, default-active) renders the role-fit summary —
  `value_alignment_notes` (CVCM "why I fit", D-33) + transferable strengths + gaps — so
  it's visible after **any** run incl. `--yes`/auto (which never pauses at the Phase-1
  checkpoint); pass `value_alignment_notes`/`skills_transferable`/`gaps` from `fit`.
- **Phase 6 `--docx` (stretch, F-33):** `phase6_docx.py` renders the SAME assembled
  markdown as `cv_final.md` into a styled `.docx`, applying formatting *conventions*
  harvested from a source CV in `data/cvs/` (body font/size, name/heading size, heading
  bold — via the table-aware `corpus.docx_loader`). Render from the markdown string, not
  the manifest, so `.docx`/`.md` can't diverge and it tests without a `RunContext`. It is
  convention-mirroring, NOT a layout clone (the tailored CV mixes sources, D-17). Keep it
  provider-free and deterministic; static text stays the person's own (D-13).

- **Experience role lines are structural, not drafted (F-29):** Phase 2
  (`_split_role_line`) peels the leading role/date line(s) off an experience section
  so the LLM never sees them (told "no heading", it drops them inconsistently);
  they're stored in `manifest[sid]["role_line"]` and re-attached bold at assembly,
  between the company heading and the body. Promotion stacks (D-21) → multi-line
  `role_line`. Never put the role line back into the draftable body.
