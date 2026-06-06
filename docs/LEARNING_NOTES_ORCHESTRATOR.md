# LEARNING_NOTES_ORCHESTRATOR.md — cv-tailor
## Architectural Decisions, Findings, and Portfolio Notes

**Project:** Week 3 Portfolio — Multi-Model Orchestration  
**Repository:** cv-tailor
**Status:** Build complete (Steps 0–9 done; UI in progress)  
**Last updated:** post-Step-9 / Sonnet validation (F-28)

---

## How to use this document

Each entry records one architectural decision: what was decided, what was
rejected, the load-bearing reason, what the pattern generalises to, and how
to frame it in an interview. This is the "why" complement to SPEC_ORCHESTRATOR.md
(the "what").

Entries are added throughout the build — when a decision is made, when a
finding changes the design, when a test reveals something unexpected.

---

## Decision Log

---

### D-01 — Hybrid pipeline with agentic refinement loop

**What was decided:**
Deterministic phases (0–2, 4–6) frame an agentic refinement loop (Phase 3).
The pipeline always runs the same phases in the same order. Phase 3 is agentic:
the orchestrator decides what critique to accept, whether to extend the rubric,
and when convergence has been reached.

**Alternatives rejected:**
- *Pure pipeline* — fixing the revision sequence removes the system's ability
  to adapt based on what critique reveals. The number of iterations is genuinely
  unknown in advance.
- *Pure agentic loop* — unpredictable costs, no defined HITL placement, harder
  to test deterministically.

**Load-bearing reason:**
Determinism where the task is known; agency where judgment is required. The
framing phases (JD parsing, initial drafting, formatting validation) have
fixed inputs and fixed expected outputs. The refinement loop doesn't — it
terminates based on convergence signals that are only computable at runtime.

**What this generalises to:**
Most production agentic systems are not pure agent loops. They are deterministic
scaffolds containing one or more bounded agentic regions. The boundary between
"always does X" and "decides whether to do X" is an architectural choice with
cost, testability, and HITL-placement consequences. Making that boundary
explicit is a design skill.

**Interview framing:**
"I deliberately split the system into deterministic phases and an agentic loop
rather than making everything agentic. The refinement stage genuinely needs
model judgment — when to stop, which suggestions to accept. The fit assessment
and validation stages don't. Conflating them is a common design mistake."

**Open question resolved during architecture review:**
The "orchestrator judges convergence" soft-stop needs a concrete trigger, not
just a free-form model judgment. Agreed resolution: soft-stop is permitted only
when the last critique returned zero major-severity items. This makes the
soft-stop testable and observable in the audit trail.

---

### D-02 — LLMs as tools: the cross-provider abstraction

**What was decided:**
The orchestrator calls other LLMs as tools, identically to how the Week 2
agent called SQLite queries. `critique_cv(draft, jd_analysis)` internally
calls GPT-4o-mini. `extract_keywords(jd)` internally calls Mistral. The
orchestrator has no visibility into which provider ran.

**Alternatives rejected:**
- *Orchestrator with direct multi-provider awareness* — the orchestrator would
  need provider-specific error handling, retry logic, and prompt formatting for
  each model. This couples orchestration logic to provider implementation.
- *Single-provider orchestration* — loses the empirically grounded rationale
  for GPT as the critique model (observed harsher feedback).

**Load-bearing reason:**
The tool abstraction makes providers swappable. If GPT-4o-mini is replaced,
only the tool implementation changes — the orchestration loop is unchanged.
This is the same reason the Week 2 tool layer existed: to isolate the
orchestrator from implementation details of its dependencies.

**What this generalises to:**
The tool pattern is not specific to function calls or API integrations. Any
system that returns structured output can be a tool — including other AI
models. This is the insight that makes multi-model orchestration composable
at scale.

**Interview framing:**
"The architecture treats LLMs as tools. The orchestrator calls critique_cv()
the same way it calls get_spending_summary() in my finance agent. The provider
is an implementation detail of the tool, not a concern of the orchestrator.
That abstraction is what makes the system composable and the providers
swappable."

---

### D-03 — Model routing: three providers, three roles

**What was decided:**

| Provider  | Model             | Role                             | Justification                                     |
|-----------|-------------------|----------------------------------|---------------------------------------------------|
| Mistral   | mistral-small     | JD extraction + embeddings       | Existing integration; cheaper structured tasks    |
| Anthropic | claude-sonnet-4-6 | Orchestrator + primary writer    | Complex multi-step reasoning; established Week 2  |
| OpenAI    | gpt-4o-mini       | Independent writer (challenger)  | Empirically harsher, more direct; different prior |
| Anthropic | claude-haiku-4-5  | Formatting validation            | Fast, cheap, sufficient for deterministic checks  |

*Updated post-D-28: GPT-4o-mini is now a writer, not a critic. Both models draft
independently per section; Claude Sonnet orchestrates adjudication. The empirical
justification for GPT (harsher, less flattering) still holds — it now shows in
draft quality differences rather than critique items.*

**Load-bearing reason for the GPT critique choice:**
This is the only genuinely novel routing decision. The justification is
empirical: direct personal observation that ChatGPT gives harsher, more
actionable CV feedback than Claude — less tendency to flatter. Using GPT
here is defensible; using it arbitrarily to "demonstrate multi-provider"
would not be. The architecture should always be able to answer "why this
model for this task?" with something better than "to add variety."

**What this generalises to:**
Model routing decisions should be grounded in observed behavioural differences,
not capability marketing. In production systems, model assignment is a
first-class architectural decision with cost and quality implications.

**Interview framing:**
"Every model in this system has a specific role justified by observed behaviour,
not by arbitrary provider diversity. GPT-4o-mini critiques because I've
directly observed it gives harsher, less flattering feedback than Claude —
which is exactly what you want from a CV reviewer."

---

### D-04 — Dynamic scoring rubric

**What was decided:**
The scoring rubric is a versioned first-class object, not a static list.
Created by Mistral at the start from the JD; extendable by the orchestrator
during refinement when critique surfaces requirements not in the original JD.
Version number increments on each update.

**Alternatives rejected:**
- *Static rubric* — measures the CV against the JD as written, missing
  inferred requirements the critique model surfaces.
- *Unversioned mutable rubric* — makes it impossible to audit which scoring
  criteria applied to which draft iteration.

**Load-bearing reason:**
A dynamic rubric measures the CV against what the JD *means*, not just what
it says. Evaluation criteria are a design decision, not a given — in any
iterative refinement system, "are we measuring the right things?" should be
as live a question as "are we improving the thing we're measuring?"

**Safeguards agreed during architecture review:**
1. Maximum 2 rubric additions per iteration (prevents runaway requirement
   inflation that stalls keyword coverage artificially).
2. Each addition must be validated against the JD by the orchestrator before
   being accepted ("is this actually implied by the JD?"). Validation decision
   logged in audit trail.
3. Items added from critique tracked as `list[RubricAddition]` with fields:
   `keyword`, `added_in_iteration`, `triggered_by` (description of the
   critique item that surfaced it). Flat list is insufficient for audit.

**What this generalises to:**
Dynamic evaluation rubrics appear in any system where the evaluation criteria
and the thing being evaluated improve together. The pattern generalises to
benchmarks that evolve during model training, acceptance criteria that expand
during code review loops, and quality gates that adapt to discovered requirements.

**Interview framing:**
"The rubric isn't static. As the critique model surfaces requirements not
explicit in the JD, the orchestrator can extend the rubric — up to two additions
per iteration, with each validated against the JD. The rubric is versioned so
you can always trace which criteria applied to which draft."

---

### D-05 — Dual-signal convergence

**What was decided:**
The refinement loop terminates when both signals plateau: keyword coverage
score (0–1, proportion of rubric items present) AND critique score (0–10,
GPT's overall assessment). Both deltas must fall below threshold, or
max_iterations is reached, or the orchestrator declares a soft-stop.

Termination table:
```
keyword_delta < 0.05 AND critique_delta < 0.5  →  convergence
iteration == max_iterations                     →  hard stop
zero major critique items (last iteration)      →  soft-stop permitted
```

**Alternatives rejected:**
- *Single keyword score* — gameable: a CV can stuff keywords while remaining
  structurally weak, and the loop terminates on a false positive.
- *Single critique score* — fragile: the critique model could give inflated
  scores while required keywords are still missing.
- *Orchestrator-only termination* — "the model decides when to stop" without
  a grounded signal is a source of non-deterministic costs and is not testable.

**Load-bearing reason:**
Two orthogonal signals provide a more robust termination condition than one.
Neither signal can be satisfied by gaming the other. The soft-stop condition
(zero major items) gives the orchestrator a legitimate early-exit path without
reducing the termination condition to pure model judgment.

**Failure modes to watch during build:**
- Score inflation: critique score rises quickly in iteration 1 (early wins),
  then stalls. Loop runs to max_iterations on minor improvements.
- Rubric expansion stall: added requirements push keyword coverage down after
  rubric update, resetting the delta counter artificially.
- Conflicting signals: keyword coverage converges but critique score does not
  (or vice versa). The loop correctly continues — but may be frustrating if
  the human can see the CV is "good enough." The HITL phase is the escape valve.

**What this generalises to:**
Dual-signal convergence is a general pattern for any loop where a single
quality metric is insufficient. It appears in training loops (loss + validation
accuracy), search (precision + recall), and any iterative refinement where
multiple orthogonal dimensions of quality matter.

**Interview framing:**
"The loop uses two orthogonal convergence signals — keyword coverage and GPT
critique score. Either signal alone can be gamed or fail silently: a CV can
score well on keywords while being structurally weak, or receive high critique
scores while missing required terms. Both must plateau before the loop exits."

---

### D-06 — Audit trail separate from context

**What was decided:**
All orchestrator reasoning is logged to `run_log.jsonl` but never injected
back into the messages array. Context stays clean; the audit trail is complete
and inspectable after the run. Same pattern as Week 2 transcript logging.

**Load-bearing reason:**
Two independent concerns: (1) keeping the context window free of accumulated
reasoning verbosity, which would inflate token costs and potentially distort
future model behaviour; (2) producing an audit trail that is readable without
reconstructing the conversation. Separating them is the correct design.

**Schema refinement agreed during architecture review:**
`ReasoningEntry` should include `rubric_version: int | None` so that score
entries in the audit trail can be traced to the rubric that was active when
the score was computed. Without this, score progression in the output is
ambiguous if the rubric changed mid-loop.

**What this generalises to:**
Observability is a first-class concern in any agentic system. In production,
the ability to audit why an AI made a decision is often as important as the
decision itself. The pattern of separating context (what the model sees) from
audit trail (what the human reads afterwards) recurs in every serious deployment.

**Interview framing:**
"Reasoning is logged to the audit trail but never fed back into context — same
pattern as Week 2. This keeps the context window clean and prevents reasoning
verbosity from inflating token costs. The HTML output renders the full audit
trail as a collapsible reasoning trace: the CV is clean, but every decision
is inspectable."

---

### D-07 — Schema additions identified during architecture review

**What was decided:**
Four schema corrections agreed before build begins:

1. **`CritiqueItem` gains `applied: bool`** — distinguish "accepted in principle"
   from "actually reflected in the next draft." Log when accepted ≠ applied.

2. **`added_from_critique` in `ScoringRubric` becomes `list[RubricAddition]`**
   where `RubricAddition` has: `keyword: str`, `added_in_iteration: int`,
   `triggered_by: str` (description of critique item). Flat list loses
   provenance.

3. **`PipelineOutput` does NOT store `drafts: list[str]`** — intermediate drafts
   are checkpointed to disk as `draft_v0.md`, `draft_v1.md`, etc. Phase 6 reads
   them from `outputs/<run_id>/` to build the Changes tab diffs. `PipelineOutput`
   is a summary object; storing all draft text in it would make it a data warehouse.
   The checkpoint pattern handles this more consistently.

4. **`ReasoningEntry` gains `rubric_version: int | None`** — so score entries
   in the audit trail can be traced to the rubric that was active when scored.
   Without this, an iteration where the rubric expanded (dropping keyword coverage
   from 0.78 to 0.71 despite real improvement) looks like a regression in the
   Scores tab. One field prevents a misleading artefact in the output.

**Load-bearing reason:**
Schema gaps that are invisible at write-time become painful at read-time.
The audit trail provenance, the scoring ambiguity, and the Changes tab diffs
all require this state to exist — but the drafts should live on disk, not in
memory, consistent with the checkpoint pattern.

**What this teaches:**
Step 0 (schemas) is more important in this project than in Week 2 because
the schemas are communication contracts *between* providers, not just between
modules. The blast radius of a post-build schema change is larger.

---

### D-11 — Critique severity labels must be prompt-defined, not just schema-defined

**What was decided:**
`CritiqueItem.severity` uses two levels: `"major" | "minor"`. The definitions
are explicit in the GPT critique system prompt (not just in the schema comment):

- **major**: materially weakens the application or contradicts a JD requirement
- **minor**: improvement opportunity; the CV is acceptable without it

**Why this must be in the prompt, not just the schema:**
The soft-stop condition depends on "zero major items from both writers." If
the writer prompts don't define `major` with precision, each model calibrates
severity on its own — inconsistently across iterations and across writers.
A "major" item from Claude in iteration 1 might be equivalent to a "minor"
item from GPT in iteration 3. The convergence condition then becomes meaningless.

*Updated post-D-28: severity is now self-assessed by both writers inside
`WriterDraft.items`, not by a separate GPT critique call. The principle is
unchanged — and more important: both writer system prompts must carry identical
severity definitions so the zero-major freeze condition is consistent.*

**Three levels considered and rejected:**
`major | medium | minor` was considered. Rejected: the soft-stop condition
would need to specify whether "zero major items" or "zero major+medium items"
triggers soft-stop eligibility, adding ambiguity. Two levels with clear
definitions is simpler and sufficient.

**What this teaches:**
When a schema field drives control-flow decisions (here: loop termination),
the values of that field need specification that lives in the prompts, not
just the type annotations. Schema and prompt design are coupled.

**Interview framing:**
"The soft-stop condition depends on critique severity labels, so the labels
had to be defined precisely in the critique prompt — not just in the schema.
If the model calibrates severity on its own, the convergence condition becomes
iteration-dependent noise."

---

### D-08 — Cost tracking at model level, not provider level

**What was decided:**
`cost_breakdown` in `PipelineOutput` tracks cost at the model level:
`{"anthropic_sonnet": x, "anthropic_haiku": y, "openai_gpt4o_mini": z,
"mistral_small": w}` rather than at the provider level.

**Load-bearing reason:**
In demo mode, the Haiku orchestrator runs instead of Sonnet. A provider-level
breakdown collapses these into a single "anthropic" figure, making it
impossible to compare demo vs full mode costs meaningfully. Model-level
tracking makes the mode comparison honest and the cost cap logic cleaner.

---

### D-09 — `--dry-run` flag behaviour

**What was decided:**
Dry-run exits after Phase 1 (fit assessment). Outputs:
- Terminal summary: JD analysis, recommended CV, fit score, gaps
- `run_log.jsonl` written with phases 0 and 1 entries only
- No `cv_final.html` or `cv_final.md` produced (dry-run is explicitly
  pre-draft)

No HITL checkpoint in dry-run — the terminal output IS the result.

**Load-bearing reason:**
Dry-run is useful for validating corpus ingestion and JD parsing before
committing to a full API spend. It should be cheap to run and immediately
readable. If it produced partial HTML output, the user would have to open a
file to see results — defeating the purpose.

---

### D-10 — ChromaDB de-duplication on re-ingestion

**What was decided:**
De-duplication key is `filename + version_date` from `CVMetadata`. On
re-ingestion without `--replace`, files with a matching key are skipped with
a warning. With `--replace`, the existing document is deleted and re-ingested.
Duplicate check runs before any embedding calls to avoid unnecessary API spend.

**Load-bearing reason:**
Silent duplicates in ChromaDB skew retrieval scores — the same CV appears
twice with slightly different embedding noise, and one of them wins the
retrieval even if neither is the best match. The de-duplication key must be
stable across re-runs (filename alone is sufficient for most cases, but
version_date catches intentional updates to the same file).

---

## Findings Log (populated during build)

> Newest first within the build order. F-17 onwards covers the dual-writer
> design (D-28–D-31) and all subsequent steps. F-17–F-20 are Step 5/6 dual-writer
> findings; F-21–F-30 cover Steps 6–9, Sonnet validation, and post-build reviews.
> F-15/F-16 (union coverage, threshold calibration) carry over unchanged and were
> re-validated under the dual-writer design (F-21, F-28).

*Entries added here when build reveals something that changes or confirms
an architectural decision. Format: what was found, which decision it affects,
what changed (if anything).*

---

### F-40 — Report-trust bugs found by reading a real report: a tool-XML leak in the fit text, an empty reasoning group, and a non-self-describing run dir (two "bugs" were a stale-regen artifact)

**Found by the user reading the F-39 Fit-tab report on a real run.** Four issues reported; triage
split them cleanly into two real product bugs, one model-hygiene bug, and two artifacts of the
no-spend regeneration helper — and that split is the lesson: *validate the diagnosis before fixing,
or you fix the wrong layer.*

**Bug 1 — tool-call XML leaked into a user-facing field (real).** `value_alignment_notes` ended
`…full effectiveness.</alignment_notes>\n</invoke>` — Haiku leaked its own tool-call/pseudo-XML
syntax into a *string value*, and phase1's `.strip()` only trims whitespace. Fix: a
`strip_tool_artifacts` helper (`helpers.py`) that strips any run of trailing XML-like tags,
applied at the **source** (phase1, to `value_alignment_notes` + `no_fit_reason`) so every consumer
(API payload → live fit panel → report Fit tab) gets clean text from one place. Trailing-only by
design (`<2 years`, `C# < C++` aren't valid tag-starts, so they survive). This is general model-output
hygiene — small models occasionally do this to any free-text tool field.

**Bug 3 — empty "?" reasoning group (real, every run).** `run_log.jsonl` holds the `run_complete`
cost footer, which has **no `phase`/`event`** (it's a different record type, read by `replay` for
cost). `_build_reasoning` bucketed it under `e.get("phase", "?")` → a blank trailing group on every
report. Fix: skip records without a `phase` *and* `event`. The footer stays in the log (replay needs
it); it's just excluded from the reasoning *view*.

**Bugs 2 & 4 — the Grounding all-clear contradicting a logged flag, and Changes showing only v0 —
were artifacts of the regen helper, not the pipeline.** The throwaway `tmp/sweep/regen_report.py`
(used to rebuild a report without re-running) loaded the **Phase-2** manifest checkpoint — whose
versions are all `0` (pre-refinement) — and passed **no** `verification_flags`. The real pipeline
passes `result.manifest` (versions updated through the loop) + `flags` to `generate_output`, so its
reports are correct. **Root cause worth fixing anyway:** the run dir wasn't *self-describing* — the
final manifest was never persisted, so a report couldn't be faithfully regenerated from disk. Fix:
`run.py` now writes a `final_manifest.json` checkpoint before Phase 6 (versions + applied formatting
corrections). The regen helper now prefers it (and reconstructs grounding flags from the run_log
`unsupported_claim` events), so a regenerated report matches a fresh one — verified on the user's run:
all four now correct (clean fit text · Grounding shows the 1 flag · no empty group · v0→v1 diffs).

**Tests:** +4 (`test_helpers` strip cases, `test_phase6` Fit-tab render + reasoning-skips-footer);
suite **252 green**. **Affects D-06 (audit/reasoning view), D-07 #3 (checkpoint self-description),
F-35 (grounding), F-39 (Fit tab); the run dir is now regenerate-able.** Takeaway: reading one real
artifact end-to-end surfaced bugs no unit test had — and half the reported "bugs" were in the
*inspection tool*, which is exactly why diagnosis precedes the fix.

---

### F-39 — Two user-driven features for the July "evaluate 20 jobs" workflow: surface the value-alignment "why I fit" in the UI, and degrade gracefully when a writer fails

**Driven by the F-37 finding** that the CVCM's standout value is the *explanation* (the
`value_alignment_notes`), not the score — "when evaluating 20 jobs I care more about *why am I a
fit* than whether the score is 0.647 or 0.612." Plus the F-38 aside that a transient GPT failure
aborted whole runs. Both target the user becoming the primary user.

**1. Render `value_alignment_notes` — in two places, because of how the user runs it.** The backend
already emitted it in the fit checkpoint payload (`api/runner.fit_payload`); it was never rendered.
- **Live fit panel** (`HitlPanel.FitBody`): a "Why you're a fit" callout above the section mix —
  for *conversational* runs that pause at the Phase-1 checkpoint.
- **Persisted report** (`templates/output.html` + `phase6_output`): a new **Fit tab** (first,
  default-active) with the value-alignment narrative + Strong alignment (transferable skills) +
  Potential gaps. This is the one that matters for the **`--yes`/auto** July workflow, which *never
  pauses at the fit checkpoint* — so the live panel alone would be invisible there. The report shows
  after any run and is the iframe the web OutputPanel embeds, so both web and standalone get it. Kept
  the clean CV on its own tab (no pollution). Falls back to `no_fit_reason` when there's no CVCM
  (`value_alignment_notes` is None without one, D-33). Validated by **regenerating an existing CVCM
  run's report from its checkpoints** (`tmp/sweep/regen_report.py`, no spend) — Fit tab + prose render.

**2. Graceful degradation on a writer failure (`phase3_refinement`).** Before: `gpt_writer` (or
`claude_writer`) raising `WriterError` after its one R-09 retry **aborted the entire run** — the F-38
JPMC validation lost a 6-minute run to one transient GPT hiccup. Now each writer call is wrapped
(`_safe_write` → draft or None on any exception, logged loudly as `writer_failed`). If **exactly one**
writer fails, the loop **degrades to the survivor**: a stand-in draft (survivor's text, empty items)
lets `adjudicate` still score/direct the section, then the selected text is forced to the survivor
*verbatim* (no synthesis reword of a single source) with honest provenance (`selected_base` set to the
survivor), the failed writer's pushback is skipped (it'd just fail again), and a `writer_degraded`
event flows to the audit log + SSE. Only if **both** writers fail is it surfaced (`WriterError`) —
genuine inability to draft, never ship a blank section. The stand-in's empty `items` means the
zero-major freeze logic counts only the survivor's items (no double-count). Best/worst case: *GPT
timeout → Claude-only → slightly lower confidence → CV still generated*, instead of a dead run.

**Tests:** +2 in `test_phase3.py` (GPT-fails → claude-only verbatim + `writer_degraded` logged; both-fail
→ `WriterError`). Suite **248 green**. Frontend `tsc -b && vite build` clean. **Deferred per the user:**
full-corpus-union grounding and a Sonnet verifier stay future enhancements (low value now).
**Affects D-33, the F-35 verifier/report, D-28 dual-writer loop, R-09; builds on F-37/F-38.**

---

### F-38 — The Goodhart fix at the metric: `keyword_coverage` now counts only SOURCE-SUPPORTED keywords — fabrication stops being a way to score (live: 5→1 flags, coverage de-inflates honestly, convergence intact)

**Why (the F-37 evidence made it undeniable):** `keyword_coverage`/`union_coverage` counted any
rubric keyword *present in the draft*, regardless of whether the candidate's CV supports it. So the
optimisation target rewarded inserting JD phrases the source doesn't evidence — textbook Goodhart.
F-34 hardened the writer prompts, the orchestrator gate, and (F-35) added the verifier, but those
*fight* a metric pointing the wrong way: the prompts say "don't insert unsupported keywords" while
the score says "more keywords = better." F-34 explicitly **deferred** the deeper fix (stop the metric
itself rewarding unsupported keywords); F-37's sweep (drift scaling with iterations, 18–19 flags/4
runs) is the evidence that it was time.

**The fix (minimal, surgical — the metric, not the loop):** `scorer.py` gains an optional
`source_text`/`source_texts`. When supplied, a keyword counts only when present in the draft **AND**
evidenced by the candidate's raw source; the denominator stays the full rubric pool, so an inserted
keyword the source can't back adds **zero** — coverage can no longer be raised by fabricating, and
max achievable coverage is bounded by what the corpus actually supports. **Opt-in by design:** no
source arg → draft-only scoring, unchanged. Phase 1 scores the *raw corpus* (text == source, no
divergence possible) so it passes none and is untouched; Phase 3 threads each section's raw source
(the `sections/<id>_source.md` from F-35) into the per-section decision coverage, the frozen-section
row, and the aggregate union. `adjudicate` maps an absent source (`""`, unit tests / no Phase-2
source) to draft-only rather than zeroing — production always has a real source (`_raw_source` falls
back to the draft, never empty). Tests: +6 in `test_scorer.py` (supported-coverage caps, surfacing
credit, union grounding, legacy-unchanged); suite **246 green**.

**Live validation (Airwallex, CVCM on, demo/Haiku, 3 iters — same config as the F-37 baseline):**

| | coverage trajectory | frozen | convergence | fabrication flags |
|---|---|---|---|---|
| **before** (F-37 Test B) | 0.571 → 0.619 (*climbing as keywords inserted*) | 0 → 1 | dual_signal | **5** |
| **after** (this fix) | 0.381 → 0.364 (*honest — fabricated kw no longer count*) | **2 → 2** | dual_signal | **1** |

Three things this confirms: (1) **flags dropped 5→1** — with no metric reward for insertion, the
loop stops manufacturing JD phrases (the remaining flag is a minor date detail, "across EMEA
(2019–2023)"); (2) **coverage de-inflated** (0.62→0.36) and that is the metric becoming *truthful*,
not a regression — it now reports the supportable fraction, so don't compare the new absolute number
to old dashboards; (3) **convergence still fires** (`dual_signal_converged`) and in fact sections
**froze earlier** (2 at iter 1 vs 0 before) — when the score isn't chasing fabricated coverage,
sections settle. The delta-based thresholds (D-05/F-16) are unaffected: they measure plateau, not
absolute level. **JPMC (the F-37 8-flag spike) confirms it harder:** flags **8→4**, coverage
**0.88→0.44**, and it now **converges at iter 2** where before it thrashed to max-iter (0.882 →
0.778 → 0.789, never plateauing). The inflated 0.88 *was* the fabricated keywords — strip them and
coverage is a stable, honest 0.438 that plateaus cleanly. Across both JDs: flags **13→5**, coverage
roughly halved (truthful), convergence improved in both. The residual flags (e.g. JPMC's invented
"€25M in Northern Europe revenue") are genuine embellishments the verifier still catches — the metric
fix removes the *systematic* incentive; the gate remains the backstop for one-off fluency drift.

**Aside (robustness obs, not this fix):** one JPMC validation run aborted on a transient
`gpt_writer.WriterError` ("no valid draft" after its single R-09 retry) — a GPT-4o-mini hiccup on one
section currently aborts the whole run. Pre-existing behaviour (surface, don't corrupt); a future
hardening could degrade gracefully (fall back to the Claude draft for that section). Logged, not fixed.

**Affects D-04, D-05, D-25; closes the Goodhart fix F-34 deferred; builds on F-35/F-37.** The trust
story is now four aligned layers: writer rules (don't) → orchestrator gate (caps fabrication) →
honest metric (no reward for it) → verifier (catches any that slips). The metric was the missing one.

---

### F-37 — Robustness sweep + CVCM A/B (8 demo runs, 4 JDs × {no-CVCM, CVCM}, 3 iters): the trust gate holds under variety; CVCM lifts fit/coherence without leaking or weakening the gate; drift scales with iteration count

**What was run:** the deferred robustness sweep, structured as a controlled A/B. 4 JDs
(`jd.txt` Airwallex / `jd2_jpmc` Fusion-AI / `jd3_ai_consultancy` Principal Solutions Director /
`jd4_figma` SC leadership) × 2 conditions — **Test A** (CVCM file moved aside, `load_cvcm()→None`)
and **Test B** (CVCM restored). Demo (Haiku), `--max-iterations 3 --yes`. 8 runs, **$2.60 total**
(~$0.33/run at 3 iters; matches F-30's $0.33 figure, not F-26's 1-iter $0.10). Test B doubles as
the current-orchestrator robustness sweep (CVCM is auto-loaded in normal operation).

| JD | A fit | B fit | A conv | B conv | A flags | B flags |
|----|-------|-------|--------|--------|---------|---------|
| Airwallex      | 0.579 | **0.684** | conv@3 | conv@2 | 6 | 5 |
| JPMC (Fusion)  | 0.529 | **0.647** | conv@2 | maxiter@3 | 1 | **8** |
| AI consultancy | 0.167 | 0.158 | maxiter@3 | conv@2 | 5 | 3 |
| Figma          | 0.474 | 0.474 | conv@2 | conv@2 | 6 | 3 |
| **mean / Σ**   | **0.437** | **0.491** | — | — | **Σ18** | **Σ19** |

**Trust findings (the P0 question — does anything fabricated slip through?):**

1. **The gate is high-precision, occasionally coarse-span — not noisy.** Across 37 flags, the
   ones inspected were *genuine unsupported additions*, not paraphrase false-positives: the
   writers inject the JD's own phrasing under keyword pressure — "4 concurrent **AI-driven**
   product initiatives" (AI-consultancy A, mirroring the JD's "3–4 concurrent AI projects"),
   "collaborating with **Data Scientists and Engineers**" / "architect for **scale, reliability**"
   (JPMC B, JD verbatim), "**SaaS**" (Figma). All real Goodhart drift; all caught. The one
   imprecision is *span*: the verifier flagged Microsoft's "contributing to **$5M** in secured
   revenue through enterprise sales cycles, including technical discovery and PoCs" as unsupported
   — but `$5M in secured revenue` **is** in the source; only the appended clause is invented. The
   flag is correct (the clause is fabricated) but the span swallows a real metric. Minor; the human
   still sees the right section. Not worth narrowing now.

2. **Under `--yes`, flagged claims SHIP into `cv_final.md` — the gate surfaces, it does not strip.**
   This is the honest trust boundary and it is by design (D-18 preview-before-apply; `--yes`/AutoHITL
   is the non-interactive escape). So the auto-run CV *contains* the flagged drift, flagged in the
   Grounding tab + Phase-4 review + CLI warning. The invariant is **"nothing ships *unflagged*"**, not
   "the draft is fabrication-free." An interactive user strips them at the Phase-4 gate; an `--yes`
   user must read the Grounding tab before sending. **Deliberately NOT auto-reverting** flagged spans
   to source: the coarse-span case ($5M) shows auto-strip would delete real content. Documented, not
   "fixed" — auto-removal is the wrong call. **Resolved (user, 2026-06-06):** `--yes` should NOT
   refuse-to-finish or hard-block on flags either — the candidate is the final gatekeeper (applies +
   final-reviews manually), so the CV ships flagged-but-produced and the human gate catches anything
   that slips. Confirms D-18; the open "(b)" from the verdict below is closed.

3. **Drift scales with iteration count.** 18–19 flags/4-runs at 3 iters vs the historical ~1 flag at
   1 iter (F-35). Each refinement pass is another chance for a fluent writer to reach for a JD phrase.
   The `TRUTHFULNESS_RULES` (F-34) + verifier (F-35) *catch* the drift but don't *prevent origination*
   — the deferred deeper Goodhart fix (stop `keyword_coverage` itself rewarding unsupported keywords,
   F-34) is now evidenced as worth revisiting. **Not done here:** the gate currently backstops it and
   the fix is a scoring-model change with its own blast radius — a decision for the user, logged below.

**CVCM A/B findings (Test A vs B):**

4. **CVCM lifts fit score, consistently where the candidate's pattern genuinely maps** (+0.105
   Airwallex, +0.118 JPMC; flat on AI-consultancy/Figma → mean 0.437→0.491). The lift is the
   Phase-1 `value_alignment_notes` doing real work: each one ties the candidate's *recurring
   cross-domain pattern* to the JD's core problem **and names the gap out loud** — JPMC's: "may
   underweight the need for deep financial services and cloud-native architecture expertise that
   J.P. Morgan's production environment demands." Honest, not flattery. This is the standout value,
   now confirmed across 4 JDs, not one (F-36 saw it on Airwallex only).

5. **The 5 target themes surface — mostly in Skills/Profile framing, not new claims.** B reliably
   reframes real content toward *building operating models* ("Designed go-to-market operating
   models"), *translating technical capability into business value* ("Translated complex technical
   capabilities into customer-facing solution strategies"), *repeatable systems* ("repeatable
   processes and delivery frameworks", "enable repeatable success"), and *solving ambiguity*
   ("execution clarity"). **Weakest-surfaced: partner-led growth** — A actually kept "partner-centric
   growth strategies" more explicitly than B. Coherence: B profiles are tighter and organised around
   a value thesis vs A's achievement list (clearest on Airwallex); keyword-stuffing improves in
   *format* (JPMC Skills: A's "·"-separated term-salad → B's narrative bullets).

6. **The format win has a fabrication tail — and it's the most important CVCM finding.** CVCM pushes
   toward a coherent *capability narrative*; where the candidate genuinely **has** the capability that
   reframes real content (passes the gate). Where the candidate **lacks** it, the same pressure
   manufactures it: JPMC B's narrative Skills assert "Architected cloud-based systems for scale,
   reliability, and operational controls" and "collaborating with Data Scientists and Engineers" —
   exactly the JD requirement the fit assessment flagged as the gap — driving flags 1→8. (Partly an
   iteration confound: A/JPMC converged at 2, B/JPMC ran 3.) Net per-JD CVCM mostly *reduced* flags
   (6→5, 5→3, 6→3) **except** JPMC, where coherence-pressure met a real capability gap. **The gate
   caught every case** — including the one borderline CVCM-echo ("Translated technical capabilities
   into client commercial objectives").

7. **No verbatim CVCM leakage in any of the 4 B CVs** (grepped the telltales: "repeatable business
   outcomes", "clarity where ambiguity", "first-principles", "successful first", "after my direct
   involvement", "boundary between technical and commercial" — all clean). The forceful
   `CVCM_FRAMING_NOTE` rewrite (F-36) holds across JDs: the value model reframes, it does not get
   copied. CVCM does **not** weaken the trust gate.

**Verdict:** the trust architecture (F-34/F-35) is robust across JD variety and CVCM on/off — it
catches genuine drift and never lets CVCM wording leak. CVCM earns its place on *fit + honest
gap-naming + narrative coherence*. **No code change made *in this sweep*:** it validates the existing
design; the two judgment calls it surfaced were left to the user — **(a) the deferred
`keyword_coverage` Goodhart fix → DONE in F-38** (source-grounded coverage); **(b) whether `--yes`
should refuse-to-finish / hard-warn when flags > 0 → resolved: NO** (the candidate is the final
gatekeeper; ship flagged, confirms D-18). **Affects D-04/D-05/D-18/D-33; builds on F-34/F-35/F-36.**
**Caveats (honest):** one run per cell (directional, not statistical); demo/Haiku only (Sonnet's
calibration, F-30, would likely drift less and converge tighter); 3-iter inflates the flag counts
vs a 1-iter demo; JPMC's A-vs-B flag delta is confounded by the extra iteration B ran.

---

### F-36 — CVCM built (§3.9/D-33): Phase-1 value alignment is the win; the writer integration leaked the model's wording into the CV until the guardrail was made forceful

**What was built:** the optional Candidate Value Creation Model (`candidate/value_creation_model.md`,
gitignored, auto-loaded). `tailor/candidate.py` loads it; it threads into Phase 1 (4th lens →
`FitAssessment.value_alignment_notes`), Phase 2 (drafting context), and Phase 3 (both writers +
the orchestrator tiebreak). Tiebreak is a **prompt instruction** to the orchestrator (it already
picks `selected_base`; there's no numeric band in code), so it only breaks near-ties and never
overrides a quality or truthfulness winner. Absent file → pipeline runs identically
(`value_alignment_notes` stays None).

**Phase 1 is the standout.** On the Airwallex JD the value-alignment note was genuinely insightful
*and honest*: it named the transferable pattern (turning API-driven capability into enterprise
adoption) **and** stated the real gap out loud — "domain expertise has been in adtech, identity,
and semiconductor IP — not payments or fintech." Exactly the value the CVCM is meant to add, with
no flattery.

**The lesson — CVCM is a fabrication vector unless the guardrail is forceful.** First live run with
CVCM in the writers: verification flags jumped **1 → 9**, several being the value model's *own
wording* lifted straight into the CV (Skills gained "API-Driven Platform Translation" and "Expertise
in translating complex, API-driven platform capabilities into solutions enterprise clients
understand…" — near-verbatim CVCM). The writers treated background context as content. The gentle
"framing only" note wasn't enough. Rewriting `CVCM_FRAMING_NOTE` to be forceful — "BACKGROUND
ONLY… NEVER copy or paraphrase any wording from the value model into the CV… if a concept isn't
already in the SOURCE it does NOT appear" — cut it back to **1 flag** (and that one was a real
catch: an invented date "deployed June 2024"). **The verification gate (F-35) caught every leak** —
which both validated the gate and was the signal that surfaced the problem.

**Decisions locked (D-33):** CVCM is **framing-only; the CV corpus stays the single fact source** —
a CVCM-derived claim with no CV backing is still flagged by the verifier (the trust gate is not
weakened by CVCM). Auto-load only (no `--cvcm` flag). The system never generates the CVCM. A
committed `*.example.md` template scaffolds candidate authoring.

**Affects:** D-33, the new schema field, Phases 1/2/3, F-34/F-35.

---

### F-35 — Trust hardening: harden Phase 2's drafter, ground against the RAW corpus, and add a verification gate (precision matters: 9→1 flags)

**Why:** F-34 fixed the Phase-3 writers, but Phase 2 — which *originates* the first draft —
used its own **weaker** prompt (not `writer_common.TRUTHFULNESS_RULES`), and the orchestrator
grounded against the *evolving draft*, not the candidate's real CV. So fabrication could be born
in Phase 2 and never caught. The user (rightly) wanted a prototype they can *trust*, not one
clean run.

**Three changes:**
1. **Harden Phase 2 (origination).** `phase2_initial_draft._SYSTEM` now shares
   `writer_common.TRUTHFULNESS_RULES` with the Phase-3 writers, so the no-headline / no-sector /
   no-unsupported-keyword rules apply from the first draft.
2. **Ground against the RAW corpus.** Phase 2 persists each section's raw source text
   (`sections/<id>_source.md`, via `RunContext(..., source=True)`); Phase 3's orchestrator and the
   new verifier both check against *that*, not the iteration draft — closing the origination gap.
3. **Verification gate + provenance (`tools/verifier.py`).** After refinement, a Haiku pass
   checks each non-static section's final text against its raw source and flags any unsupported
   claim as a major item that flows into the Phase-4 review (the human gate), the audit log, a
   **Grounding tab** in the HTML report, a `fabrication_flags` count in the summary, and a CLI
   warning. Nothing ships unflagged that the candidate's own CV doesn't say.

**The precision lesson (the important one):** the FIRST verifier prompt was a "strict
fact-checker" and produced **9 flags on a real run — most FALSE POSITIVES**, flagging claims that
were *verbatim* in the source (e.g. "15-person … $25M business and 20% YoY growth", which is in
the profile source word-for-word). A gate that cries wolf gets ignored — worse than no gate.
Rewriting the prompt to flag ONLY **new checkable facts** (a metric, employer, title, sector,
named system) and to treat rewording / paraphrase / dropped qualifiers as SUPPORTED — with an
explicit "find the supporting span first; when unsure, don't flag" method — cut it to **1 flag**
(a genuine vague addition) on the same JD. Cheap-model fact-checking is viable but only with a
precision-first prompt; "list unsupported claims" is far too trigger-happy for Haiku.

**Verified:** real Airwallex demo run — no fintech/payments/identity fabrication, correct titles,
1 low-noise flag surfaced in the report. **Affects D-04/D-05/D-18/D-25; builds on F-34.**

**Residual / deferred:** verification grounds each section against ITS OWN source variant, so a
true claim that lives only in a *different* variant or section could in principle be flagged
(none observed after the precision fix). A future hardening could ground against the union of a
section across all CV variants. In full mode the verifier could use Sonnet for higher precision
(it currently uses the fixed Haiku validation model). One subtle miss remains: a near-synonym
term-swap ("solutions consulting" → "solutions engineering") isn't flagged — borderline, not a
clear fabrication.

---

### F-34 — The keyword-coverage incentive drove fabrication; fixed with stronger writer rules, a reframed keyword block, and a source-grounded orchestrator gate

**The failure (real Airwallex run, demo):** the tailored CV invented a "Solutions
Engineering and Pre-Sales Leader" headline on the profile and *every* experience block,
recast "Senior Product Manager" as a solutions-engineering role, and claimed **fintech /
payments / financial-services** sector experience the candidate doesn't have (their domains
are adtech / identity / semiconductor). Root cause: the rubric's `required_keywords` are the
JD's phrases verbatim, and `keyword_coverage` against them is a **scored optimization target**
(drives drafting + convergence). A fluent model maximises a counted metric by *fabricating* —
classic Goodhart. The `TRUTHFULNESS_RULES` were a soft instruction losing to a hard metric,
and three specific gaps let it through:
1. the keyword list read as a **checklist to insert** ("RUBRIC required keywords: …");
2. the writer rules didn't forbid the actual moves seen — injecting a role headline/tagline,
   relabelling a role's nature, or claiming a *sector* not in the source;
3. **the orchestrator adjudicated "truthfulness" blind** — `adjudicate()` got only the two
   drafts, never the SOURCE, so it couldn't tell a fabricated claim from a real one and a
   fluent fabrication scored well and converged.

**The fix (A+B+C, prompt + one plumbing change; no metric overhaul):**
- **A.** `TRUTHFULNESS_RULES` (writer_common, shared by both writers in **Phase 2 and 3**)
  now explicitly bans headlines/taglines, role-identity/seniority assertions, sector/domain
  relabelling, and inserting any JD keyword the source doesn't already evidence ("unsupported
  keyword = fabrication, not coverage").
- **B.** `jd_rubric_block` reframed from "required keywords" → "a relevance guide, NOT a
  checklist; use a term only where the source evidences it."
- **C.** `adjudicate()` now receives `source_text` (the section the writers drafted from) and
  is told to judge truthfulness FIRST: fabrication caps a draft at 4/10, blocks `converged`,
  and the direction must cut the unsupported content. The source rides in the user message,
  not the cache prefix (it varies per section, D-31).

**Verified:** a fresh demo run on the same JD — headline injection gone, titles truthful
(Solution Consulting, not Solutions Engineering), no fintech/payments claims, Skills grounded,
bullets restored. **Affects D-04/D-05/D-25; the core anti-fabrication invariant.**

**Caveats / deferred:** C grounds against the *iteration's input* (the Phase-2 draft on
iter 1), not the raw corpus section — A+B prevent origination upstream, so the pair holds, but
a future hardening could persist the original corpus text as the ground truth. The deeper
Goodhart fix (stop `keyword_coverage` itself from rewarding unsupported keywords) was
explicitly deferred — revisit if fabrication resurfaces.

---

### F-33 — Stretch: `--docx` by harvesting the source CV's formatting conventions, rendered from the assembled markdown

**What was built:** `tailor/phases/phase6_docx.py` + a `--docx` flag. It writes
`cv_final.docx` (clean CV only) by (1) harvesting formatting *conventions* from a source
CV in `data/cvs/` — body font/size, name size, heading size, heading-bold — and (2)
rendering the **same** assembled markdown `assemble_markdown` produces for `cv_final.md`
into styled Word paragraphs. Chosen over an in-place clone of one source file because the
tailored CV reorders and mixes sections from several CVs (D-17), so no single source's
layout represents it; conventions transfer, exact layout doesn't.

**Why render from the markdown, not the manifest:** the assembled markdown is the
canonical clean CV (§9). Rendering the docx from that same string guarantees `.docx` and
`.md` never diverge, and decouples the renderer from `RunContext` — so it unit-tests
against a built-in-process fixture `.docx`, no corpus and no API (5 tests).

**Real-CV smoke (no spend) confirmed the harvest handles the real document family,
which the fixture can't:** on `CV_…_Airwallex.docx` it read Calibri/11pt body (the font
is a **theme** font — `Normal.font.name` is None — so harvesting falls back to the modal
*run* font, which resolves it), an 18pt name, 16pt headings, and `heading_bold=False`.
That last one is the corpus's documented quirk (F-04: "section headers and company names
here are sized, not heading-styled") — the harvester reproduces *sized, not bold*
headings rather than imposing bold, which is the whole point of mirroring conventions.

**Reuse:** `corpus.docx_loader.load_docx` (table-aware — CV content lives in one table)
supplies the size/bold/bullet signals; nothing new parses .docx. Provider-free,
deterministic. **Closes the SPEC "Stretch — docx output" item.**

---

### F-32 — UI Step 6: prod overlay — Compose concatenates `volumes`, and dev/prod must not share an image name

**What was built:** `frontend/Dockerfile.prod` (multi-stage: `node:20-alpine` builds
the Vite bundle → `nginx:1.27-alpine` serves it, no Node in the final ~50 MB image),
`frontend/nginx.conf` (SPA `try_files` fallback + `/api/` proxy to `backend:8000` with
`proxy_buffering off` for SSE), and the completed `docker-compose.prod.yml` overlay.
Smoke-tested: `/` → 200 SPA, `/api/health` → proxied 200, arbitrary route → index.html.

**Finding 1 — Compose CONCATENATES the `volumes:` list across `-f` overlays; it can't
subtract.** The dev frontend bind-mounts `./frontend:/app` + an anonymous
`/app/node_modules` for hot reload. A prod overlay `volumes: []` is a **no-op** —
`docker compose config` showed the dev mounts still present in the merged result.
Resolved by *not fighting it*: the mounts are **inert** under nginx (it serves
`/usr/share/nginx/html`, the baked bundle — never `/app`), so the production bundle is
what ships. Verified by serving the built stack, not by reasoning. (A
`docker-compose.override.yml` split would remove them, but that contradicts SPEC §7.5's
two-file structure and changes the documented dev flow — not worth it.)

**Finding 2 — dev and prod frontend services must NOT share an image name.** Both
default to `cv-tailor-frontend` (Compose `<project>-<service>`), so a prod build
**clobbered** the dev Vite image — a later `docker compose run --rm frontend npm run
build` then hit nginx's entrypoint (`npm: not found`). Resolved with an explicit
`image: cv-tailor-frontend-prod` on the prod service. The backend doesn't have this
problem (dev and prod build the *same* Dockerfile, so the shared name is correct).
**Affects SPEC §7.5/§12.6** (the overlay example lacked both the explicit image name
and the volumes note).

---

### F-31 — UI Step 4: conversational HITL is the SAME handler interface, paused over HTTP; preview-before-apply needs a multi-turn loop

**What was built:** `api/runner.SSEHITL` implements the pipeline's existing HITL
handler interface (`fit` / `review` / `formatting`) — the *same* contract as
`AutoHITL` (tests/`--yes`) and `TerminalHITL` (CLI). Nothing in `tailor/run.py`
changed: the pipeline already delegated checkpoints to a handler (F-26, the
injectable-handler decision), so the Web UI was a third front end, not a new code
path. Each method publishes a JSON checkpoint payload via `Session.wait_hitl(...)`
and BLOCKS the pipeline thread until `POST /api/runs/{id}/hitl` calls
`submit_hitl(...)` (the cross-thread handoff already existed from UI Step 1).

**Finding 1 — preview-before-apply (D-18) forces a multi-turn checkpoint, not a
single request/response.** The CLI's Phase-4 free-text path is *interpret → show
back → "apply? y/n" → execute*. A single blocking `wait_hitl` can't express that:
the human must see the Haiku interpretation **before** the revision runs. Resolved
by making `SSEHITL.review` a **loop** — `apply_item` / `interpret` (→ sets a
`preview`, re-publishes) / `apply_freetext` (confirm) / `accept` (exit) — each turn
re-`wait_hitl`s with the updated state. Interpretation (Haiku) and revision (Claude
writer) run **on the paused pipeline thread**, reusing `phase4_hitl.interpret_freetext`
/ `revise_section` verbatim — no new tool, no provider code in the endpoint. Fit and
formatting are single-turn (binary decisions); only section-review needs the loop.

**Finding 2 — `auto` defaults to OFF (interactive), the inverse of the build note.**
The resumption note suggested "AutoHITL … default-on." But conversational HITL is
the *point* of Step 4 and the portfolio demo, so a default-on auto would hide it.
Resolved: `POST /api/runs` takes `auto: bool = False`; the frontend has an
"Auto-run (skip my review)" checkbox (unchecked by default) for the start-to-finish
path. AutoHITL is still one flag away, so demo runs that shouldn't pause still can't
strand on a checkpoint. **Affects the §12.6 UI Step 4 plan; builds on F-26.**

**Reused, not rebuilt:** `phase4_hitl.{interpret_freetext,revise_section,unresolved_list}`,
`phase1_fit_assessment.interpret_fit_response` (new, mirrors the Phase-4 interpreter
for proceed/stop), and `phase5_validation.render_corrections`’s data. Tests exercise
the handshake with **zero real API calls**: the fit pause/resume goes through the live
`SSEHITL.fit` (button path, no LLM); the review loop mocks only `revise_section`.

---

### F-30 — Haiku vs Sonnet (D-26): the bigger model buys calibrated JUDGMENT, not better output, at this task scale

**The controlled run (Airwallex, dual-writer loop, `max_iterations=3`, current code):**

| | Haiku (demo) | Sonnet (full) |
|---|---|---|
| iterations run | 3 (hit the cap) | **2 (converged)** |
| convergence | `max_iterations` (never plateaued) | **`dual_signal_converged`** |
| quality trajectory | 7.63 → 7.69 → **8.25** (climbing) | 6.98 → **7.08** (flat) |
| keyword coverage | 0.895 → 0.947 → 0.895 | 0.947 → 0.947 |
| Δq at last iter | **+0.562** (> 0.5 → can't converge) | +0.100 (< 0.5 → converges) |
| cost (est.) | **$0.33** | $0.79 |
| cost / iteration | ~$0.11 | ~$0.40 (**~3.6×**) |
| outcome | partial | partial |
| output (profile/skills) | strong, on-brief | strong, on-brief |

**The finding — the value is judgment, not output.** On the *artifact*, Haiku is
competitive: its tailored profile/skills surface the same JD concepts (payments,
fintech, EMEA, pre-sales, RFP, PoC), read well, and hit the same 0.947 coverage.
Where Sonnet earns its ~3.6×-per-iteration premium is **calibration**:

1. **Sonnet knows when to stop; Haiku doesn't.** Sonnet's scores are conservative and
   *stable* (6.98 → 7.08), so the dual-signal plateau fires and it converges at iter 2.
   Haiku scores *higher and keeps climbing* (7.63 → 8.25) — its Δq stays above the 0.5
   threshold, so it never converges and runs to the cap. This is the R-08 LLM-judge
   over-scoring tendency, live: the smaller model is the more optimistic, more volatile
   judge, which makes its convergence signal noisier. Sonnet is the harsher, better-
   calibrated scorer — exactly the property D-05’s termination logic depends on.
2. **Cost crossover is subtler than "Sonnet is dearer."** Per iteration Sonnet costs
   ~3.6× more, but it *converges in fewer iterations*. To actually settle, Haiku needed
   a 4th iteration (at iter 3 only 5/8 sections had frozen, Δq still +0.56) — so
   run-to-convergence Haiku ≈ $0.44 vs Sonnet $0.79. The gap is ~1.8×, not 8× (the raw
   token-price ratio), because the better judge does less redundant work.
3. **Quantitative metrics hide the gap.** Both land at coverage 0.947 and produce a
   submittable CV; a metrics-only dashboard would call them equivalent. The real
   differences are convergence behaviour, score calibration, and (F-12) outcome
   steadiness — none visible in the headline numbers. *This is itself the lesson:* for
   "is the bigger model worth it?", the honest evidence is in process quality
   (does it know when it's done?), not the output score.

**Practical conclusion (confirms D-26):** dev/iterate on **Haiku** — it's ~⅓ the
cost and the output is genuinely close. Reserve **Sonnet** for the final pass where
calibrated convergence and a steady verdict matter. The demo defaulting to Haiku is
the right call; Sonnet is a deliberate, justified upgrade, not a default.

**Caveats (honest):** single JD, one run per model (directional, not statistical);
the Sonnet datapoint is F-28 (pre-title-fix, but that touches only experience role
lines, not the profile/skills/scores compared here). A multi-JD, repeated-run sweep
would harden the cost-crossover number. **Affects D-26, D-05, R-08; builds on F-21/F-28.**

---

### F-29 — Experience role/date line is structural — split it out at draft time, re-attach at assembly (the drafter drops it)

**What was found (reviewing the F-28 Sonnet CV):** Microsoft’s experience block had
**no job title**, and the two AppNexus/Xandr role-groups (D-21) rendered as two
identical `## Appnexus / Xandr` blocks — the second looked missing. Root cause: an
experience section’s source leads with a plain-text role/date line (`Senior Product
Manager (Apr 2022 – Mar 2024)`), and the Phase 2 drafter is told "Output ONLY the
section text … no heading". The LLM treats that first line as a heading and **drops
it inconsistently** — Microsoft lost it, Utiq kept it. Because the CV heading is the
company alone (F-23, "the role is already in the body’s bold line"), a dropped role
line leaves the section titleless and makes co-employer role-groups collapse.

**Fix (deterministic, no fabrication risk):** the role line is a *fact*, not
something to refine — so it never enters the draftable text. `phase2._split_role_line`
peels the leading non-bullet line(s) off an experience section before drafting; the
LLM rewrites only the bulleted body; the verbatim role line is stored in
`manifest[sid]["role_line"]` and re-attached (bold) between the heading and body at
assembly (`phase6.assemble_markdown`). The writers (Phase 3) only ever see the body,
so they can’t drop it either. Promotion stacks (D-21: several role lines before the
shared bullets, e.g. Director + Associate Director) are captured as multi-line
role_line and each rendered bold. Also taught `_md_to_html` to render `**bold**` as
`<strong>` so the role lines show in the HTML CV tab (was literal `**`).

**Why this is the right shape:** it’s the same principle as static sections (D-13)
and header rendering — *deterministic where the content is a fact, LLM only for
judgment/wording* (D-01). The role line carries the job title and dates; those must
survive verbatim, and "ask the model nicely to keep it" is not a guarantee. Now it
is one. **Verified live (demo, all 5 experience sections):** every role-group shows
its title+dates; the two AppNexus/Xandr groups are distinct; the Director group
shows both stacked roles. **Affects F-23, D-21, the Phase 2 manifest contract, and
SPEC §5 Phase 6.** Tests: +4 (`test_phase2`, `test_phase6`); suite 187 → 191.

---

### F-28 — Sonnet validation (D-26): dual-signal convergence FIRED; freezing did nothing; synthesis is responsive, not a reflex; cost far under estimate

**What was run (the deferred D-26 final validation):** full mode — **Claude Sonnet**
writer + orchestrator, GPT-4o-mini writer, Mistral Phase 0, Airwallex JD,
`max_iterations=3`, `--yes`. One live run. The whole point was to see the
Haiku→Sonnet delta and watch the things F-21 deferred.

| it | coverage | Δkw     | quality | Δq      | newly_frozen | active | selected bases (of 8) |
|----|----------|---------|---------|---------|--------------|--------|------------------------|
| 1  | 0.947    | +0.105  | 6.975   | +0.000  | 0            | 8      | synthesis 8            |
| 2  | 0.947    | +0.000  | 7.075   | +0.100  | 0            | 8      | synthesis 4, claude 4  |

Converged at **iteration 2** by `dual_signal_converged` (Δkw 0.000 < 0.05 AND Δq
0.100 < 0.5) — it did NOT run to the cap. **Cost $0.79 estimated** (Sonnet $0.764,
Haiku $0.018 formatting/validation, GPT $0.009, Mistral $0.0003).

**Five things this validated or corrected:**

1. **Dual-signal convergence actually fired (resolves the F-21 deferral).** F-21’s
   demo run hit the iteration cap before convergence could be observed; here, with a
   third iteration available, the loop converged *on its own* at iter 2 because both
   signals plateaued. The termination table (D-05) is now observed end-to-end, not
   just unit-tested.

2. **Freezing did ZERO work — yet the loop still terminated.** `section_frozen`
   events: **0**. Sonnet’s orchestrator never set `converged=True` on any section
   (an even stricter bar than Haiku’s — F-21 froze 2, F-16’s single-writer loop froze
   5–6). So every section was re-drafted both iterations, and the *only* thing that
   stopped the loop was the aggregate dual-signal plateau. This is the strongest
   possible evidence for D-05’s two-independent-paths design: when the per-section
   freeze path contributes nothing, the aggregate-plateau path is the safety net that
   prevents running to the cap. A single-signal or freeze-only design would have run
   the full 3 iterations here for no quality gain.

3. **Synthesis is responsive, not a reflex (resolves F-21’s watch item).** Demo/Haiku
   synthesised 14/16 (87.5%) and the worry was synthesis-as-default. Sonnet:
   iter 1 **8/8 synthesis**, iter 2 **4/8 synthesis + 4/8 claude** → 12/16 (75%),
   and the *trend* is what matters: as the two drafts converge across iterations the
   orchestrator increasingly takes one verbatim instead of merging. Early divergent
   drafts → synthesis; stabilised drafts → pick. That trajectory is what "earned, not
   reflex" looks like. (Claude-verbatim picks rose; GPT was never picked outright on
   this JD — consistent with Claude-as-primary-writer, D-03/D-28.)

4. **The rubric JD-validation guard (D-04) is load-bearing under Sonnet.** Sonnet’s
   orchestrator proposed **30+** rubric additions across the run; `rubric.py`
   **rejected every one** as "not implied by the JD" (0 accepted, rubric stayed v1).
   Coverage was already 0.947, so unchecked additions would only have inflated the
   denominator and made the score dishonest. The verbose-critique-inflates-the-rubric
   failure mode D-04 predicted is real and model-dependent (Haiku barely proposed
   any; Sonnet proposes freely) — the cap + JD-validation is exactly what keeps the
   convergence signal meaningful. *Calibration note:* a few proposals named real JD
   concepts (EMEA scope, payments domain) yet were rejected as already-covered/not-core;
   the guard errs conservative, which protects the score — acceptable, worth a glance
   if a future JD genuinely needs a mid-loop addition and never gets one.

5. **Steadier verdict + cost far under estimate.** Outcome **partial** (fit 0.579,
   7 typed gaps) — the steady, nuanced read F-12 predicted Sonnet would give, vs
   Haiku’s STRONG/PARTIAL wobble on this borderline JD (D-26 delta confirmed).
   And **$0.79 ≪ the ~$2–4 D-28 estimate**: because it converged at iter 2 (not 3)
   on 8 active sections, real full-mode spend on this corpus is sub-$1. The D-28
   estimate was conservative; the realistic figure for an Airwallex-scale run is
   ~$0.8. **Affects D-05, D-04, D-26, D-28; resolves the F-21 deferrals.** cv_final.md
   = 968 words (under the two-page envelope, F-25).

---

### F-27 — Step 9: the deferred end-to-end test — one mock seam, fakes that count

**What was built:** `tests/test_phases.py` — a fully-mocked `run_pipeline` pass
(Phase 0→6, `AutoHITL`, demo mode), the E2E deferred from Step 8. 6 tests; suite
181 → **187**, still zero API calls.

**The seam that kept it small:** the pipeline already isolates every provider SDK
behind three getters (`get_{mistral,anthropic,openai}_client`) and the corpus
behind `all_sections` (F-26, R-05). So the *entire* run is faked by monkeypatching
four names — `phase0`'s imported `get_mistral_client`, `helpers.get_anthropic_client`
/ `get_openai_client` (which `claude_complete`/`gpt_complete` resolve at call time),
and `run.all_sections`. One prompt-aware Anthropic fake dispatches on `tool_choice`
name to serve six call sites (fit / draft / decision / pushback / rubric /
formatting) plus the tool-less Phase 2 draft; one OpenAI fake serves the GPT writer;
one Mistral fake serves Phase 0 — reusing the `SECTION TYPE:`/tool-name dispatch
already proven in `test_phase3`. **The clean tool/provider boundary (D-02) is what
makes a 7-call-site, 3-provider pipeline testable through 4 setattr lines.**

**Cost accuracy without brittle hardcoding (§9 item 3):** the fakes emit *fixed*
per-call usage (Anthropic 1000/100, OpenAI 500/50, Mistral 200/20) and **count
calls** into a shared `rec` dict. The test then recomputes the expected footer from
`rec × PRICES_USD_PER_MTOK` and asserts it equals `cost_breakdown_estimated_usd` —
exact, yet robust to a change in how many times each provider is called (it reads
the actual count, not a magic 13). This verifies the whole helpers→cost→footer
chain end-to-end, not just `CostTracker` arithmetic (which `test_cost` already
covers in isolation). The footer-shape assertion also pins the implemented §9 keys
(`cost_breakdown_estimated_usd` / `total_estimated_usd` / `total_estimated_gbp` /
`note`), closing the SPEC §9 example drift.

**Freeze determinism asserted explicitly (§8):** the pipeline is run twice with
identical fakes; `iteration_1.json`'s per-section `converged` flags + `sections_
converged`/`active` match exactly (same input → same freeze). **Replay** is driven
through the real click CLI (`CliRunner`) against the produced run dir, confirming it
reads phase0/phase1 checkpoints + `iteration_*.json` + the `run_complete` footer +
reasoning trace. **Affects D-08, §9; closes the Step 9 E2E gap (test-coverage note).**

---

### F-26 — Step 8: pipeline + CLI — central cost capture and an injectable HITL handler

**Two design choices that kept Step 8 small:**

1. **Cost is captured once, in `helpers`, not threaded through every tool.** Every
   provider call already funnels through `claude_complete` / `gpt_complete` /
   `embed_texts`, so each notes its usage into an active `CostTracker`
   (`cost.track()` context, set by `run.py`). The dual-writer loop makes ~24+ calls
   per iteration across 4 tools — threading a usage return out of each would have
   touched every signature. A side-channel (like the audit log, D-06) is the right
   shape: the orchestrator/phases stay oblivious. Cost is **model-level** (D-08) and
   **estimated** list-price (F-08) — the footer says so explicitly and is never an
   invoice. Cached tokens fold into the input estimate (caching is a no-op anyway,
   F-22).
2. **HITL is an injectable handler, not inline `input()`.** `run_pipeline` calls
   `hitl.fit / .review / .formatting`; `TerminalHITL` reads stdin, `AutoHITL`
   accepts everything (`--yes` / tests). The phases only ever *render* (Phase 1's
   `render_fit_hitl`, Phase 4's `render_section_review`, Phase 5's
   `render_corrections`) — so the whole pipeline is testable without a TTY, and the
   same code serves the CLI and (later) the web backend's conversational HITL.

Mode is config, not branching (D-08): `resolve_run_config` maps demo→Haiku/1-iter,
full→Sonnet/3-iter, key-gated on `FULL_MODE_KEY` (§3.7). `--dry-run` stops after
Phase 1 with phases 0–1 logged and no CV (D-09). **Affects D-08, D-09, §3.7, §6.**

**Verified live (`python -m tailor run --jd data/jd.txt --demo --yes`):** Phase 0→6
end-to-end, `cv_final.md` + `cv_final.html` written, footer **$0.1045 estimated**
(`anthropic_haiku 0.1023`, `openai_gpt4o_mini 0.0022`) for one demo iteration.
`replay` reproduces role/fit/iteration progression/cost from the checkpoints +
`run_log.jsonl`. **Gap found and fixed:** Phase 0's Mistral call is the one provider
call that doesn't go through a cost-noting helper (it uses the Mistral client
directly via `call_with_retry`), so it was missing from the breakdown; `run.py` now
notes its returned usage (Phase 1+ self-note via `claude_complete`/`gpt_complete`, so
noting only Phase 0 avoids double-counting). Dry-run footer now correctly shows
`mistral_small 0.000314` alongside `anthropic_haiku`. (Cleaner long-term: a
`mistral_complete` helper so Phase 0 routes through the central capture point like
the others — deferred, low value since Mistral is free-tier.)

---

### F-25 — Step 7: validated live end-to-end (Phase 0→6)

**What was verified (Airwallex JD, Haiku, 1 iteration, demo):** the full pipeline
runs Phase 0→6 and writes `cv_final.md` (738 words) + `cv_final.html` (32 KB). The
assembled CV is correct — header rendered without a heading, then Profile → Core
Skills → experience companies in chronological order (Utiq → Microsoft → Appnexus),
real tailored content. Phase 4 renders section status with company titles and lists
18 unresolved writer items — and the items are genuinely useful (the writers flag
that the Microsoft role is PM-not-presales and Xandr is adtech-not-fintech: real fit
tensions, surfaced for the human, exactly what HITL is for).

**Two dynamics confirmed:** (1) **per-section vs assembled length are different
checks** — individual sections were flagged over their per-section `max_words`
(AI Projects 63/70w vs a 44w budget) while the assembled total (705w) sits under the
two-page envelope (1188w); both checks are needed. (2) With `max_iterations=1`, few
sections converge (zero-major + orchestrator bar), so the unresolved list is long —
expected; a full 3-iteration run resolves most. **Confirms D-28, D-14; Step 7 gates
met.**

---

### F-24 — Step 7: Phase 4/5 adapted to the dual-writer model (HITL sources, free-text execution, formatting scope)

**What was found / decided** — the SPEC's Phase 4/5 prose predated the dual-writer
rewrite, so three things needed resolving and recording:

1. **"Unresolved items" come from the writers' self-assessment.** The dual-writer
   loop has no separate critique object, so Phase 4's unresolved list is the open
   `CritiqueItem`s (both writers, last iteration) on sections that never converged
   — surfaced via a new `RefinementResult.unresolved` (deduped by issue text). The
   "quality" progression line is the **selected** draft's quality, not a single
   critique score.
2. **Free-text [e] executes as one Claude writer pass.** Haiku interprets the human's
   text into `{section_id, instruction}` (shown back first — preview-before-apply,
   D-18), then `tools/claude_writer.write_section` runs with the instruction as its
   `direction`. The SPEC said "Sonnet executes" — same tool, model from `RunConfig`
   (Haiku/dev, Sonnet/full, D-26); no new revise tool.
3. **Phase 5 formats non-static sections only.** Static sections are the person's
   verbatim content (D-13) — left untouched. The assembled-length envelope is the
   sum of per-section `max_words`; accepted corrections are written as the next
   version so Phase 6's "highest version" assembly picks them up. **Affects D-13,
   D-18, D-28.**

---

### F-23 — Step 7: Phase 6 assembly order under section-mixing (manifest carries position + title)

**What was found:** SPEC §5 Phase 6 said "order by `CVSection.position` from the base
CV metadata" — but there is no single base CV under section-mixing (D-17): each
section can come from a different CV, and positions across CVs aren't comparable.

**Decision:** order by **(config `cv_sections` type index, then source `position`)**.
section_type is the primary, reliable key (header → profile → skills → experience →
…); `position` is only a within-type tiebreak, which matters mainly for the
experience block. Cross-CV experience ordering is imperfect but deterministic — good
enough for a first cut; the Phase 1 HITL section-mix (and a future reorder control)
can refine it. To keep Phase 6 **checkpoint-driven** (D-07 #3 — no corpus re-query at
output time), the Phase 2 manifest now carries `position` and `title` per section
(alongside `section_type`), the same enrichment pattern used for Phase 3. **Affects
D-17, D-07 #3.**

**Follow-up (display disambiguation):** experience role-groups are split per
company AND per role (D-21), so two role-groups at one employer (e.g. AppNexus /
Xandr: Director + Solution Consultant — the `/` is the acquisition rename, kept on
purpose) would collapse to one identical line in the status / Changes / Scores
displays, which show only a label. The manifest now also carries a `label`
(`"{company} — {role}"` for experience, else the title); the CV **heading** stays
the company alone (the role is already in the body's bold line, so the CV body was
never ambiguous), while the status/table displays use `label`. The corpus already
had both fields — experience metadata carries `company` and a role `title`
(sectioniser); Phase 2 was just taking `company` and dropping the role.

---

### F-22 — Step 6 (dual-writer): prompt caching wired correctly but a no-op at this prompt scale (measured)

**What was measured (probe, Haiku, two identical back-to-back calls):**

| prefix | input tok | cache_create (call 1) | cache_read (call 2) |
|--------|-----------|-----------------------|----------------------|
| real writer prefix (system + role/JD/rubric) | 534 | 0 | 0 |
| 4202-token control | — | 4202 | 4202 |

The control proves the wiring: a stable prefix over the minimum writes the cache on
call 1 and reads it 100% on call 2. But our **real** prefix is only ~534 tokens —
under *both* Anthropic minimums (Sonnet 1024, Haiku 2048) — so caching does **not
engage** at current sizes, on either tier (not just Haiku, as earlier assumed).

**Decision:** keep the wiring. It is correct, costless when below the minimum (no
error, `cache_creation == 0`), engages automatically if prompts grow (longer JDs /
system prompts / more rubric keywords), and OpenAI caches qualifying prefixes with
no code. **Do not pad prompts to force a hit:** the only cacheable bulk is the
~534-token prefix; the variable content (the two drafts, the source section) is what
dominates token cost and can't be cached, so even an active cache would save
sub-cent per iteration. The SPEC's earlier "~60% input cost reduction" framing is
withdrawn — it was an assumption; this is the measurement.

**Affects D-31.** Portfolio angle: the honest version of "add caching" is to wire it
correctly *and measure it*, then report a no-op rather than claim a saving — the
same measure-don't-assume discipline as R-08/F-14 (score anchors) and F-10 (scorer
on real data). How Anthropic (explicit `cache_control`, advisory) and OpenAI
(automatic, prefix-based) differ is itself the learning.

---

### F-21 — Step 6 (dual-writer): loop validated live; thresholds hold; synthesis dominates selection

**What was verified (Phase 0→3, Haiku writer+orchestrator + GPT-4o-mini writer,
Airwallex JD, 2 iterations, demo):**

| it | coverage | Δkw     | quality | Δq     | frozen | active |
|----|----------|---------|---------|--------|--------|--------|
| 1  | 0.947    | +0.316  | 6.625   | 0.000  | 0      | 8      |
| 2  | 0.905    | −0.043  | 7.875   | +1.250 | 2      | 6      |

All gates pass: valid per-section `IterationScore`; selected text + per-writer
drafts on disk; scores discriminate (`claude_quality ≠ gpt_quality` in 4/8
sections); freezing, rubric extension (→ v2), and rejected-minor forwarding (45
carried) all work. **Thresholds unchanged (F-16 holds under dual-writer):** Δq
+1.25 correctly did NOT converge (genuine improvement, loop hit the iteration cap),
and coverage dipped −0.043 from rubric expansion — the bounded effect D-05 predicts.

**Three real dynamics of the dual-writer design:**
1. **Synthesis dominates selection (14/16).** The orchestrator overwhelmingly
   merges the two drafts rather than picking one outright — strong evidence for the
   two-draft premise (D-28: a richer output space than one draft + a punch list).
   *Watch:* 14/16 is high enough to suspect a synthesis-as-default bias; confirm at
   the Sonnet validation (D-26) that synthesis is earned, not a reflex.
2. **Freezing is slower than the single-writer loop** (0 froze in iter1 here vs 5–6
   in F-16). The orchestrator's `converged` bar is stricter — both drafts strong
   AND zero major — so sections take longer to settle. More iterations of real work,
   higher cost: exactly the ~$2–4 trade-off D-28 accepted.
3. **Quality rises faster** (+1.25 in one iteration vs the single-writer loop's
   sub-1.0 steps) — the orchestrator's per-section `direction` carried forward (D-30)
   is doing visible work.

**Deferred → now resolved (F-28):** the 3-iteration Sonnet validation ran and dual-signal
convergence fired at iteration 2; the synthesis-bias watch (14/16 here) resolved —
Sonnet synthesises 12/16 and the share *drops* across iterations (earned, not reflex).
**Affects D-28, D-05; confirms F-16.**

---

### F-20 — Step 6 (dual-writer): Anthropic does not hard-enforce a tool's `required`; tolerate a missing array

**What was found (live, Haiku writer):** the `submit_draft` tool schema lists
`items` as `required`, but Haiku returned a tool call with `text` and no `items`
key — and the run crashed on `data["items"]`. Unlike OpenAI strict `json_schema`
(which enforces `required`/`additionalProperties` server-side), Anthropic tool
input schemas are advisory: the model usually honours them but may omit an
optional-looking array, especially a small model.

**Decision / what changed:** read tool arrays defensively — `data.get("items") or
[]`, never `data["items"]`. The validation loop already treated empty items as
valid (a draft with zero self-flagged issues is legitimate), so the only bug was
the unguarded access. Both writers fixed. **Affects R-09** (validate-before-use):
for Anthropic, "validate" must include normalising absent optional fields, not
just range/enum checks. GPT's writer is safe because strict mode guarantees the
key — a concrete reason the strict-vs-advisory provider difference matters.

---

### F-19 — Step 6 (dual-writer): `rejected_suggestions` carries MINOR suggestions only

**What was found / decided:** D-30's `rejected_suggestions` ("prevent
re-litigation") is under-specified — the dual-writer flow has no explicit
item-level accept/reject (the orchestrator selects whole drafts, not items). To
make it concrete and safe, the loop forwards into `rejected_suggestions` the
`suggestion` text of **minor** items only; **major** items are never added.

**Load-bearing reason:** freezing and the soft-stop depend on *zero major items*,
so a major issue MUST stay raisable every iteration until it's actually resolved —
suppressing it via "already considered" would let a section freeze with an open
major gap. Minor items are exactly the recurring nag D-30 targets ("add team
size" raised every round), so suppressing repeats of those is safe and useful.
**Affects D-30.** Flagged as a calibration point — confirm on real runs that
minor suppression doesn't drop a genuinely worth-doing improvement.

---

### F-18 — Step 6 (dual-writer): the orchestrator produces synthesis text; it is NOT stored on OrchestratorDecision

**What was found:** SPEC §4 has `selected_base: "synthesis"` and Phase 3 says the
"selected/synthesised text [is] written to disk", but `OrchestratorDecision`
carries no text field. Where does the merged text come from?

**Decision:** `adjudicate()` returns `(OrchestratorDecision, selected_text)`. For a
pure claude/gpt pick, `selected_text` is that draft's text **verbatim** (the model
never rewrites a chosen draft — no drift). For `synthesis`, the orchestrator writes
the merged text in a transient `final_text` tool field, which becomes
`selected_text`. The decision object stays a pure summary — **draft text lives on
disk, never in a summary schema (D-07 #3)** — so this respects the checkpoint
pattern rather than bloating the schema. **Affects D-28, consistent with D-07 #3.**

---

### F-17 — Step 6 (dual-writer): deterministic length items apply to BOTH writers

**What was found / decided:** the length-budget `CritiqueItem`s (D-14: major over
`max_words`, minor under `min_words`) are computed in code (`writer_common.length_items`)
and appended to **both** writers' drafts, tagged with `source_writer`. The updated
spec described this only for `gpt_writer`, but a length violation must reach the
orchestrator's zero-major freeze check regardless of who wrote the over-length
draft — otherwise a long Claude draft could freeze a section that breaks the
two-page limit. Code counts words for both; the models judge content. **Affects
D-14/D-28.**

---

### F-01 — Step 0: package is `tailor/`, not `orchestrator/` (build-prompt vs spec discrepancy)

**What was found:** The build-session opening prompt referred to the package as
`orchestrator/orchestrator.py` and `orchestrator/__main__.py` in its Step 8.
SPEC §7 (directory tree), §8, and every CLI example (`python -m tailor run`)
use `tailor/`. The spec is the source of truth.

**Decision affected:** none re-opened — this is a naming reconciliation, not a
design change. **What changed:** the package is `tailor/`; the main orchestration
loop lives in `tailor/run.py` (per §7's tree, which is more authoritative than
§8's prose mention of `tailor/tailor.py`). Recorded so the discrepancy isn't
re-litigated mid-build.

---

### F-02 — Step 0: generic type-hint-driven deserialiser instead of per-class `from_dict`

**What was found:** The 16 schemas nest deeply (`PipelineOutput` → `FitAssessment`
→ `dict[str, SectionRecommendation]`; `IterationScore` → `dict[str, SectionScore]`;
`ScoringRubric` → `list[RubricAddition]`). Hand-writing `from_dict` per class
would be 16× the boilerplate and 16× the places to get a nested reconstruction
wrong.

**Decision affected:** D-07 (schemas are cross-provider contracts; round-trips
must be correct). **What changed (implementation, not architecture):** a single
`Serializable` mixin provides `to_dict`/`to_json`/`from_dict`/`from_json`.
`from_dict` reads the dataclass's resolved type hints and recursively coerces
JSON values — handling nested dataclasses, `list[X]`, `dict[str, X]`, and
`X | None`. Unknown keys are ignored (forward-compatible reads); missing required
fields raise `TypeError` (asserted in tests). 49 tests cover round-trips for
every schema plus the D-07/D-11 correction guards.

---

### D-23 — Seniority is a soft ranking preference, not a hard pre-filter (to confirm at Step 3)

**What was decided (provisional; confirm against real JDs when retrieval is built):**
Phase 1's seniority signal **ranks** candidate CVs but never **excludes** them.
SPEC §3.8 calls seniority a "pre-filter"; this refines that to a soft, band-based
preference rather than a hard gate — encoded in `config.yaml` under `retrieval`.

**Load-bearing reason (user/Claude feedback during Step 1 sidecar authoring):**
Application Engineer / Deployment Specialist roles at AI-native companies often
carry no clean seniority signal in the JD — titled "Senior" or "Principal" but
scoped at what would be director-equivalent elsewhere. A hard seniority filter
would wrongly drop a strong generic CV. This is most acute for `cv_type: generic`
CVs, whose `target_role` deliberately spans levels (the corpus has three such CVs,
all `seniority: director` but written to suit principal→VP-equivalent roles).

**Mechanism (provisional):** a `seniority_order` ladder
(`senior < principal < director < vp`) yields a band distance; within ±1 step is
treated as a full match, beyond that a graded penalty — applied to ranking only.
`seniority_filter_mode` is `soft` for both cv_types initially; tighten
`job_specific` to a harder filter only if real runs show false positives.

**Links:** depends on [[F-06]] (single scalar seniority) and the canonical
vocabulary in D-22. Revisit in Step 3 (Phase 1 fit assessment) — added to Open
Questions.

---

### F-16 — Step 6: refinement loop validated live; convergence thresholds confirmed (Open Question #1 resolved)

**What was verified (Phase 0→3, Haiku orchestrator + GPT-4o-mini critique, real
JDs, demo wiring):** two real runs produced clean, well-behaved convergence:

| JD        | active | it1 Δkw | it2 Δkw | it3 Δkw | Δcrit (it1→3) | frozen/iter | reason |
|-----------|--------|---------|---------|---------|---------------|-------------|--------|
| Airwallex | 8      | −0.032  | +0.050  | +0.017  | 6.75→6.0→6.0  | 5 / 1 / 1   | dual_signal @3 |
| JPMC      | 10     | +0.235  | +0.065  | 0.000   | 6.30→6.0→6.0  | 6 / 1 / 2   | dual_signal @3 |

**The thresholds (kw_delta < 0.05, crit_delta < 0.5) are confirmed — no change.**
Real "still-improving" iterations show Δkw in 0.05–0.235; the plateau iteration
shows Δkw ≤ 0.017. The 0.05 line sits cleanly in that gap. Both runs held through
iteration 2 (genuine improvement) and converged at iteration 3 when both signals
flattened — consistent with `max_iterations: 3` for full mode (the loop reaches
natural convergence right at the cap on this corpus).

**Three real dynamics observed, all matching D-05's predicted failure modes — and
all absorbed by the dual signal:**
1. **Critique score drifts DOWN as sections freeze.** Aggregate `critique_score`
   is the mean over *active* sections only (D-12); once the easy sections freeze,
   the mean is taken over the harder survivors, so it can fall (6.75→6.0) even as
   the CV improves. `abs(critique_delta) < 0.5` tolerates this without a false
   "regression" — and it is why the signal must be paired with keyword coverage,
   never used alone.
2. **Coverage can dip slightly (Airwallex it1: −0.032)** from rubric expansion +
   rewording (the rubric grew v1→v3, adding keywords to the denominator). Net
   coverage still rose (0.63→0.667). Confirms the "rubric expansion stall" mode is
   real but bounded by the max-2-additions/iteration cap (D-04).
3. **Freezing makes later iterations cheaper and focused (D-12 confirmed):** 5–6
   of 8–10 sections froze after iteration 1; iteration 2+ critiques only the 1–4
   survivors. Verified on disk: frozen sections stop gaining version files (most
   sections end at v1, the never-converging `ai_projects` reaches v3).

**Resolves Open Question #1.** Affects D-05 (thresholds) and D-12 (freeze
economics). No code change — the calibration *validates* the pre-set thresholds.

---

### F-15 — Step 6: IterationScore.keyword_coverage is UNION coverage, not a mean (spec ambiguity resolved)

**What was found:** SPEC §4 labelled `IterationScore.keyword_coverage` a "weighted
mean across non-static sections". But a mean of *per-section* coverages is
structurally low — each section covers a different subset of the rubric, so most
per-section coverages are 0.1–0.3 and their mean would sit far below the SPEC
§Phase-4 example progression (`61% → 74% → 83%`). That example, and the CV-level
metric established in F-11, are **union coverage** (fraction of the rubric covered
*anywhere* across the non-static sections).

**Decision:** the aggregate `IterationScore.keyword_coverage` is `union_coverage`
across the current text of all non-static sections — continuous with Phase 1's
`composed_coverage` (the draft's coverage picks up where fit assessment left off)
and the right scale for the 0.05 delta threshold (F-16 confirms 0.6–0.95 ranges).
Per-*section* `SectionScore.keyword_coverage` stays as defined (this section's
coverage of the whole rubric). SPEC §4 comment updated to match.

**Affects:** D-05 (the convergence signal's scale), the SPEC schema comment. No
decision reopened — a labelling fix grounded in F-11 and the SPEC's own example.

---

### F-14 — Step 5: critique score anchors work — weak 3.0 vs strong 8.0, no over-scoring

**What was verified (GPT-4o-mini, live, Airwallex rubric):** a deliberately weak
profile ("hard worker, good communicator, various roles") scored **3.0** with a
`major` item; a strong tailored profile scored **8.0** with only a `minor`. The
score discriminated sharply and — the key R-08 risk — GPT did **not** inflate the
weak draft to 8+. The explicit anchors ("9–10 = …, 5–6 = …, 3–4 = …") in the
prompt are what prevented the LLM-judge over-scoring; without them this signal
would be useless for convergence.

**Confirms:** D-11 (severity calibrated and consistent: weak→major, strong→minor)
and the soft-stop precondition (strong draft = zero major items → soft-stop
eligible). GPT-4o-mini's harshness (D-03) shows: it gave a 2/10 section score
where a flattering model would not.

**Design notes:** structured output uses OpenAI **strict `json_schema`** — the
severity `enum` is enforced server-side, so a bad severity can't reach the
soft-stop logic. `section_scores` is a list (not a dict) because strict mode needs
`additionalProperties:false`; converted to a dict after parsing. Length-budget
items (D-14) are appended **deterministically** in code (word count), not left to
GPT — code counts, GPT judges content. Output still schema-validated + retried
once (R-09).

---

### F-13 — Step 4: draft target is anchored to source length, not the section_type median

**What was found:** First end-to-end draft (Haiku) inflated a 23-word
`design_engineer` role to **143 words** because the per-`section_type` experience
budget uses the corpus *median* (108) as target — and the drafter dutifully padded
the terse early role toward it. Padding a CV section to hit a word count is a
fabrication risk, the one thing the drafter must never do.

**Decision (D-27, resolves the Phase-2 budget open question):** the drafting
target for a section is `clamp(source_word_count, budget.min_words,
budget.max_words)` — anchored to what the source actually says, with the budget as
guardrails. Tailoring reweights wording; it must not materially change length. The
median `target_words` is kept only as a corpus statistic / the assembled-length
check (Phase 5), not as a per-section drafting target. Re-verified: drafted
lengths now track source (101→92, 152→129, 63→50) with no inflation, while
keyword coverage still lifts base→draft (0.47→0.63; 0.53→0.74 on another run).

This is the concrete resolution of the "experience budget granularity" open
question raised when D-21 split experience per role-group.

---

### F-12 — Step 3: Phase 1 validated end-to-end with Haiku; honest gap typing; outcome wobble noted

**What was verified (Phase 0 → Phase 1, Haiku/dev, real JDs):**
- **Airwallex (good fit):** section-level mix genuinely mixes sources (profile
  from Figma, skills/experience from AI/Airwallex); gaps centred on
  payments/fintech *domain* (the real gap) — addressable, no blocker.
- **JPMC (the "gap but worth applying" JD):** PARTIAL with exactly the right
  major-but-addressable gaps (hands-on cloud AI at scale, production AI with data
  scientists, cloud-native in finance) and **nothing blocking** — matching the
  human read. Seniority was NOT raised as a gap on either (D-23 honoured), even
  though the JPMC JD has no explicit title.

**Design confirmations:** the hybrid split works — deterministic composition +
coverage as inputs, Claude for typed `FitGap`s and outcome (D-01). Structured
output is forced via an Anthropic **tool** (`tool_choice`), more reliable than
free-form JSON from Haiku, then schema-validated with one retry (R-09).

**Caveat (feeds D-26):** Haiku's *outcome* label wobbled between STRONG and
PARTIAL across runs on the borderline Airwallex case (temperature=0; the gap list
stayed stable). Acceptable in dev; the final Sonnet validation should give a
steadier, more nuanced verdict — a concrete data point for the Haiku→Sonnet delta.

---

### F-11 — Step 3: keyword coverage selects section variants but does NOT rank whole-CV fit (validates the hybrid)

**What was found (Airwallex JD vs the 7-CV corpus):** Per-CV non-static union
coverage ranked **Figma 0.58 > JPMC 0.53 > AI / Airwallex / Mistral 0.47 >
Adtech 0.42**. The CV hand-tailored for *this* Airwallex role sits mid-pack —
two other solutions-leadership CVs use more of the rubric's vocabulary. Yet at
the section level the Airwallex CV's *profile* (0.32) and *skills* (0.16) score
best of all variants, while the strongest *experience* block is JPMC's (0.53).

**Implications:**
1. **Section-mixing has real value here (D-17 confirmed):** different CVs win
   different sections, so a single-base-CV pick would leave coverage on the table.
   And because all 7 are one person's career, mixing phrasings is coherent, not
   Frankenstein.
2. **Keyword coverage alone must NOT decide fit outcome.** It discriminates
   *variants of the same section* well, but as a whole-CV fit signal it's weak
   and even mis-orders (tailored ≠ highest keyword density). This is exactly why
   Phase 1 keeps deterministic coverage as an *input* and lets Claude reason the
   outcome/gaps (D-01 hybrid). Coverage feeds selection + the rubric; judgement
   feeds the verdict.
3. Cross-variant selection uses **keyword coverage, not semantic similarity** —
   semantic scores barely differ across variants of the same person's section
   (same content, reworded), whereas coverage tracks the tailoring.

---

### D-26 — Dev with Haiku, final-validate with Sonnet (orchestration cost discipline)

**What was decided (user):** Build and iterate every Claude-using phase in **demo
mode (Haiku orchestrator)** to keep dev cost near-zero, then run a few **final
validation passes in full mode (Sonnet)** once the pipeline works end-to-end, to
see the quality/value delta. This is exactly the demo/full split already in
`config.yaml` (D-08, §3.7) — no new mechanism, just a working practice: phases
take the orchestrator model from `RunConfig`, never hardcode it.

**Portfolio angle:** the Haiku→Sonnet final comparison is the orchestration-tier
analogue of the Phase 0 small-vs-medium evaluation (F-09) — a documented,
evidence-based view of what the bigger model buys, rather than assuming it.

---

### F-10 — Step 2: keyword matching must be token-subset, not exact-phrase (found on real data)

**What was found:** The first scorer matched a rubric keyword only as a contiguous
token run. Scoring the **Airwallex CV — which is tailored for the Airwallex JD —**
against that JD's own rubric gave a union coverage of **0.11** (2/19). Not a real
gap: Phase 0 emits multi-word keywords ("go-to-market strategy", "executive
communication", "rfp responses") whose words appear in the CV reworded or
non-adjacent, so exact-phrase matching missed them. A convergence signal this
brittle is useless — every section would look uncovered and deltas would be noise.

**Decision (D-25):** A keyword matches if EITHER its tokens are contiguous OR all
its *significant* tokens (minus a small stoplist) appear as whole tokens anywhere
in the text. Single-token keywords still require an exact whole-token match (no
stemming, no partials) to avoid false positives. Re-scored: the Airwallex CV now
reads **0.42** union coverage, matching the concepts it genuinely covers and
flagging real gaps (`fintech`, `payment ecosystem` = domain gaps; `proof-of-concept`,
`rfp responses` = addressable). This required/missing split is the raw material
for Phase 1's `FitGap` typing (D-16/D-17).

**Why it matters (R-08 link):** an LLM-or-heuristic score is only useful if it
discriminates consistently. The fix was found by *running the scorer on real CV ×
real JD*, not by unit tests on synthetic strings — which all passed at 0.11.
Verify scorers against real data, not just fixtures.

---

### F-09 — Step 2: Phase 0 uses mistral-small-latest + a classification/keyword prompt fix (open question resolved)

**What was tested:** `analyse_jd` run with `mistral-small-latest` vs
`mistral-medium-latest` across 4 real JDs (Airwallex, JPMC, AI-consultancy,
Figma), temp=0, identical prompt. Compared classification accuracy
(required vs nice-to-have, against each JD's explicit markers), keyword quality,
structural-requirement specificity, latency, and cost.

**Findings:**
- **Cost is not a factor.** Phase 0 is one call per run (~1.5k in / ~0.5k out).
  Aggregate over 4 JDs: small ≈$0.0011, medium ≈$0.0055 (paid list); **$0 on the
  free tier either way**. Latency: small ~2.5–3s, medium ~9–13s (**~4× slower**).
- **Medium classified cleaner** out of the box (respected "preferred"/"advantageous"
  markers); small over-populated nice_to_haves with responsibilities/values.
- **But medium's `structural_requirements` were near-identical boilerplate across
  all 4 JDs**, while small's were JD-specific and actionable (e.g. Figma "31
  consultants, 4 managers") — and structural_requirements feed drafting. The
  smaller model produced the more *useful* output there.
- `required_keywords` (the actual scoring driver) was solid in both.

**Decision (D-24):** Use **mistral-small-latest** for Phase 0, plus two prompt
rules that close small's only real gap: (a) classify by the JD's own markers
("preferred"/"advantageous"/"a plus" → nice-to-have; responsibilities → required),
(b) exclude generic standalone keywords (`product`, `engineering`, `ai`-alone) and
require JD-specific structural_requirements. **Re-validated on all 4 JDs:**
classification now matches medium's precision, zero generic-keyword leaks,
structural guidance still tailored. Net: medium's accuracy at small's speed/cost.
Revisit only if a future JD class regresses.

---

### F-08 — Cost figures are list-price ESTIMATES, not actual billing (Mistral runs free-tier)

**What was found:** The ingestion "cost" (≈$0.0011) is computed in code as
`tokens / 1e6 × list_price`, not read from any billing API. The Mistral account
is on the free "Experiment" tier (rate-limited, no payment method) so actual
spend is **£0**; the estimate overstates it by assuming the paid rate.

**Affects D-08 (cost tracking).** When the per-model `cost_breakdown` and the
`run_complete` footer are built (Step 8/9), label them as **estimated**
(list-price), e.g. `estimated_usd`, and state the assumed per-model rates in one
place. The portfolio value is "what this would cost at scale on paid tiers" — but
it must never read as a real invoice. Anthropic/OpenAI calls in later steps *are*
paid (those keys are on paid accounts), so the estimate matters there; Mistral
stays free.

---

### F-07 — Step 1: mistralai 2.4.9 puts the SDK under `mistralai.client` (RAG import path still valid)

**What was found:** The installed `mistralai==2.4.9` has **no top-level
`__init__.py`** — `import mistralai` yields an empty namespace package
(`__file__ is None`). The real SDK lives under `mistralai.client`: the client is
`from mistralai.client import Mistral`, the base error is
`mistralai.client.errors.MistralError` (with `SDKError`, `NoResponseError`,
`ResponseValidationError` beneath it), and HTTP status/headers are on
`exc.raw_response` (an `httpx.Response`), not on the exception directly.
Embeddings: `client.embeddings.create(model="mistral-embed", inputs=[...])` →
`resp.data[i].embedding`, with token usage on `resp.usage`.

**Why it matters / what changed:** This is the same import path the Week 1 RAG
helper used (`from mistralai.client import Mistral`), so that reuse holds — but
the RAG retry logic keyed on a flat `MistralError.status_code`/`.headers`, which
in 2.4.9 must be read from `exc.raw_response`. `tailor/helpers.py`'s
`call_with_retry` reads status/headers defensively across SDK shapes
(`raw_response` first, then a flat `status_code`/`headers`) so the same wrapper
will also cover the Anthropic/OpenAI clients added later. `requirements.txt`
pinned `mistralai>=2.0.0,<3.0.0` to match. Verified by introspection, no API call.

---

### F-06 — Step 1: sidecar metadata uses single scalar values, validated at write-time

**What was found:** The first hand-filled sidecar (Adtech Consulting, a *generic*
CV) packed ranges into single fields: `seniority: principal, director, VP`,
`target_role` as a comma-list, and a `target_company` despite `cv_type: generic`.
`seniority: principal, director, VP` is valid YAML but parses as the *string*
`"principal, director, VP"` — it silently would not match any seniority filter.

**Decision (D-22):** Sidecar filter fields (`cv_type`, `target_role`,
`seniority`, `target_company`) are **single scalar values**, because ChromaDB
metadata is scalar — lists can't be stored or filtered. A generic CV's breadth
is carried by its embedded content and by semantic retrieval, not by cramming
multiple values into a filter field. Controlled vocabularies: `cv_type ∈
{generic, job_specific}`, `seniority ∈ {senior, principal, director, vp}` (added
`vp`). `target_company` is `null` for generic CVs. `skills_emphasis` is the one
list field.

**What changed:** Added `validate_sidecar(data) -> (errors, warnings)` to
`corpus/metadata.py`, called by `load_sidecar` (raises on errors). This applies
R-09 (validate structured input at write-time, not downstream) to *human*-authored
input — and matters more here because the user will batch-generate the remaining
sidecars with an LLM: the validator turns a silent retrieval-time mismatch into
an immediate, fixable error. A generic-CV-with-company is a warning (surfaced at
ingest), not a hard error.

---

### F-04 — Step 1: the CV corpus has NO heading-style structure — D-15's parse assumption is wrong; reuse the RAG table-aware loader

**What was found:** The 7 real CVs (`data/cvs/`) are **table-based**: each is a
single table, all body content lives in its cells, and `python-docx`'s
`doc.paragraphs` sees only 1–2 top-level `Normal` paragraphs. A heading-style
parser (D-15) would have produced a near-empty corpus **without crashing** —
exactly the R-01 silent-partial-parse failure mode. Even inside the table,
hierarchy is not reliably heading-styled: section headers are mostly
`Heading 1`/16pt but "Core Skills" is `Heading 4`/14pt, and "AI Projects" is
`Heading 1`/16pt in some CVs and `Heading 4`/14pt in others. Company names
collide with that 14pt band (`Heading 3`, `Heading 4`, and `Normal`-bold all
appear at 14pt).

**Decision affected:** **D-15 (heading-style parsing) is corrected.** Also the
SPEC §3.8 canonical section list (see F-05).

**What changed:**
1. **Reused the Week 1 RAG `docx_loader.py`** (`corpus/docx_loader.py`) rather
   than reinventing. Its table walk (read *every* cell, pair the date column),
   `numPr` bullet detection, and rendered-size resolution (run → style →
   base-style chain) are exactly what this corpus needs — it is the same family
   of documents that logic was built for. Simplified: dropped the PDF/format-
   agnostic split (cv-tailor ingests only `.docx`) and the `source_format` field.
2. **Parsing is now fingerprint-based, not heading-based** (the RAG R-10
   "discover structure, don't assume it" pattern). The robust section-boundary
   signal is **text matched against a canonical-section vocabulary**, not style
   or size — because style/size are inconsistent but the section *titles* are a
   small known set.

**What this teaches (portfolio):** The single most valuable thing Step 1 did was
*look at the data before writing the parser*. The R-01 note predicted this exact
failure; running a 30-line discovery dump in the container turned a predicted
risk into an observed fact and saved a silently-wrong corpus.

---

### F-05 — Step 1: section model grounded in the observed corpus (vocabulary + size split; two new canonical sections)

**What was found (the corpus fingerprint):**
- Body text 11pt; name "Michel Guillon" 18pt-bold; contact lines 10pt (above
  "Profile").
- Section headers match a known vocabulary: Profile, Core Skills, Work
  experience, (Technical &) AI Projects, Education, **Languages**, Interests.
- Inside Work experience, **company = 14pt** (any style), **role line = ≤12pt**
  (carries a date, inline or in the date column), **bullet = `numPr`**. A company
  can hold several roles (Imagination Technologies has 3); bullets attach to the
  company block, not cleanly to individual roles.

**Decisions made (refining SPEC §3.8, recorded before writing the sectioniser):**

- **D-19 — Section detection = canonical-name vocabulary + size split.** A
  paragraph is a section header iff its normalised text matches a canonical
  alias *and* it is non-bullet and visually elevated (size > body OR
  Heading-styled OR bold). Within the experience block only, a new company
  sub-section starts at each non-bullet paragraph at the block's max non-bullet
  size (14pt here); role lines (≤12pt) and bullets are that company's content.
  Aliases live in `config.yaml` (`section_aliases`) — the discovered vocabulary,
  persisted, never re-guessed at runtime (R-10).

- **D-20 — Two canonical sections added: `header` and `languages`.** The spec's
  list started at `profile`, but every CV has a name/contact block above it and
  a Languages section below. `header` (position 0) and `languages` are added,
  both **static**. `certifications` stays in the vocabulary though absent from
  this corpus (present-only assembly handles its absence). Static set for this
  corpus: `{header, education, languages, certifications, interests}`; active
  (critiqued): `{profile, skills, experience_<company>, ai_projects}`.

- **D-21 — Experience sub-sections are per company AND per role-group.**
  *(First proposed as per-company; revised after user feedback — roles are worked
  on as distinct sections across the LLMs, so they should be separate.)* SPEC §3.8
  said "one CVSection per job per company". Naive per-job splitting fails on this
  corpus because companies stack promotions before shared bullets (Appnexus:
  Director → Associate Director → 4 shared bullets; Imagination: Senior Customer
  Engineer → Application Specialist → 4 shared bullets) — splitting those orphans
  the bullets. Resolution: split on **role-group** boundaries — a new section
  starts at a role line that *follows a bullet*; consecutive role lines with no
  bullet between them stay together. This gives per-role granularity while
  keeping promotion-stacks intact. `section_id =
  experience_<company>_<first-role-slug>` (e.g.
  `experience_appnexus_xandr_director_solution_consulting`). Observed result: the
  AI CV goes from 4 company sections to 7 role-group sections. Cost rises modestly
  but is bounded — sections freeze once converged (D-12), so critique stays focused.

---

### F-03 — Step 0: Docker is the run target; image pins Python 3.13-slim (supersedes the 3.12 note)

**What was found:** Mid-Step-0, the spec was extended (§6, §7, §7.5) to make
**Docker the deployment target** for the M720q homeserver. §6 now routes every
CLI command — including `pytest tests/` — through `docker compose run --rm cli`.
The `Dockerfile` pins `python:3.13-slim`.

**Decisions affected:** none re-opened, but two consequences recorded:
1. **Python version:** the container is **3.13**, not 3.12. Local dev here is
   3.13.7, so dev and prod now match. The "Python 3.12" line in earlier docs is
   superseded by the Dockerfile. (The schema code uses 3.10+ `X | None` unions and
   `typing.get_type_hints`, both fine on 3.13.)
2. **Verification command:** Step 0's gate is now run as
   `docker compose run --rm cli pytest tests/`. Verified: image builds cleanly on
   3.13-slim (requirements.txt resolves with no conflicts — chromadb 1.5.9,
   anthropic 0.105.2, openai 2.40.0, mistralai 2.4.9, pytest 9.0.3; `tenacity`
   pulled in transitively, useful for `call_with_retry` later), and 49 tests pass
   in-container.

**What changed (scaffolding):** added `Dockerfile`, `docker-compose.yml`
(cli + backend services sharing one image), `docker-compose.prod.yml` (backend
overlay), `.dockerignore`, and `ADAPTING.md`. The `frontend` service and prod
multi-stage build are defined in SPEC §7.5 but deferred to the UI phase and
gated behind comments so `docker compose build` doesn't fail on the
not-yet-existing `frontend/Dockerfile.dev`. The SSE `proxy_buffering off` nginx
note (from the RFI project) is captured as a comment in the prod overlay so it
isn't lost before the UI build.

---

## Cost Tracking (populated during build)

All figures are **list-price estimates** (F-08), not billed; Mistral runs free-tier.

| Run | Mode | Mistral | Anthropic Sonnet | Anthropic Haiku | OpenAI | Total USD |
|-----|------|---------|------------------|-----------------|--------|-----------|
| Airwallex, **3 iter (hit cap, no convergence)** — Haiku side of the D-26 comparison (F-30) | demo (Haiku) | 0.0003 | — | 0.3172 | 0.0128 | **0.3304** |
| Airwallex, full Phase 0→6, **2 iter to dual-signal convergence** (D-26 validation, F-28) | **full (Sonnet)** | 0.0003 | **0.7636** | 0.0178 | 0.0089 | **0.7906** |
| Airwallex, full Phase 0→6, 1 iter (Step 8 live) | demo (Haiku) | 0.0003 | — | 0.1023 | 0.0022 | **0.1045** |
| Airwallex, dry-run (Phase 0→1) | demo (Haiku) | 0.0003 | — | 0.0059 | — | **0.0062** |

The full-mode Sonnet run came in at **$0.79**, well under the ~$2–4 D-28 estimate
(F-28): it converged at iteration 2 (not the 3-iteration cap) on 8 active sections,
so the conservative estimate overstated realistic spend on this corpus by ~3–5×.
Haiku here is only the formatting/validation gate (Sonnet is the writer+orchestrator).

---

## Test Coverage Notes (populated during build)

*Which behaviours are tested deterministically (pytest), which require LLM-gated
tests, and which are tested by inspection only.*

- **241 tests, all deterministic / mocked (no API).** Every provider is faked;
  LLM behaviour is validated by live driver runs recorded as findings (F-12, F-14,
  F-16, F-21, F-25, F-26), not in the pytest suite.
- **Schemas** (test_schemas, 46): round-trips + D-07/D-11/D-28 guards.
- **Tools** (test_writers, test_orchestrator, test_rubric, test_scorer): each
  dual-writer/orchestrator/rubric/scorer tool in isolation with mocked providers.
- **Phases** (test_phase0/1/2/3/4/5/6): per-phase, mocked. Freeze logic is
  deterministic (same input → same freeze).
- **End-to-end** (test_phases, 6 — Step 9, F-27): a fully-mocked `run_pipeline`
  pass (Phase 0→6, all three SDK providers in one run). Asserts cv_final.md/.html,
  a complete run_log (spine events + footer), exact cost footer from known token
  counts, freeze determinism, and `replay`. Closes the Step 8 E2E gap.
- **Pipeline** (test_cost, test_run): cost math + helpers→cost wiring; RunConfig
  mode-gating; HITL handlers.
- **docx stretch** (test_docx, 5 — F-33): harvest conventions from a fixture .docx;
  render assembled markdown to styled paragraphs (name/heading/role/bullet, **bold**
  stripped to bold runs, List Bullet style); end-to-end harvest→render; template
  resolution. Provider-free, no real CV.
- **Web UI** (test_api): route shape; corpus endpoints (ChromaDB faked); run
  initiation + SSE replay; archive/replay + downloads; Session primitives (event
  buffer/seq, TTL, the thread handoff). Conversational HITL (UI Step 4): the
  `SSEHITL` handshake — fit pause/resume + stop through the live handler, the
  multi-turn review loop (apply-item and free-text preview→apply, revision mocked),
  the binary formatting checkpoint, `auto` skipping pauses, and `hitl_ready` reaching
  the browser over the live SSE stream while paused — all with zero real API calls.

---

## Open Questions (resolved before closing the project)

- [x] ~~Does the convergence threshold (keyword_delta < 0.05, critique_delta < 0.5) need
      calibration after seeing real iteration data?~~ **Resolved (F-16):** validated on
      two real runs (Airwallex, JPMC). Thresholds unchanged — real improving iterations
      show Δkw 0.05–0.235, plateau iterations Δkw ≤ 0.017; the 0.05 line sits cleanly in
      the gap. Both converged by dual-signal at iteration 3.
- [x] ~~Is `mistral-small` the right model for Phase 0, or does structured extraction
      quality warrant `mistral-medium`?~~ **Resolved (F-09/D-24):** small + prompt
      fix, validated on 4 JDs — medium's accuracy at small's 4× speed and lower cost.
- [x] ~~**Experience budget granularity (Phase 2/3):** a single `experience`
      target over-inflates small role sections.~~ **Resolved (D-27/F-13):** draft
      target = `clamp(source_word_count, min, max)`; the median is a corpus stat /
      Phase-5 check, not a per-section drafting target.
- [x] ~~**D-23 — seniority soft filter:** confirm the soft/band-based seniority
      ranking (not hard pre-filter) against real Application Engineer / Deployment
      Specialist JDs when Phase 1 retrieval is built.~~ **Resolved (F-12):** Phase 1
      validated on real JDs (Airwallex, JPMC) — seniority was NOT raised as a gap on
      either, including the JPMC JD that carries no explicit title, so the generic
      `seniority: director` CVs were never excluded. `build_where` (corpus/retrieval)
      deliberately omits seniority from the ChromaDB `where` filter; `_SYSTEM` in
      Phase 1 instructs Claude to treat seniority as a SOFT signal. The soft-filter
      decision is confirmed; `seniority_filter_mode` stays `soft` for both cv_types
      (tighten `job_specific` only if a future run shows a false positive).
---

## Reuse Analysis — What the RAG and RFI Projects Teach This Build

*Synthesised from LEARNING_NOTES.md (Week 1 RAG pipeline) and LEARNING_NOTES_RFI.md (RFI Answer Builder) before the Week 3 build began. Each entry maps a prior finding to a concrete implication for the orchestrator.*

---

### R-01 — docx parsing: heading styles are not guaranteed, and silent failures are the dangerous kind

**From:** RAG LEARNING_NOTES Phase 2 ("real documents lie about their structure")

**What was found:** A Word document's visual hierarchy and its underlying markup are two different things. The RAG project's CV used proper heading styles for sections but direct formatting (font size) for company names — a naive style-only parser missed them silently, producing wrong-but-plausible chunks with no error.

The fix was a fingerprint profiler that discovers structure rather than assumes it. But the key lesson for this project is the failure mode: **the pipeline ran, produced output, and was wrong. Nothing crashed.**

**Implication for the orchestrator — Section boundary detection:**
The orchestrator's ingestion step must not assume heading styles are present or consistent across all 6 CV .docx files. Ingestion must verify section extraction output explicitly: after parsing, print a section inventory (`section_id: N words`) and require human confirmation before proceeding. A CV that parsed to 2 sections instead of 8 must fail loudly, not silently produce a 2-section corpus.

Specifically from the RAG notes: "company names aren't heading-styled at all, and one company shares the exact style used for job titles." This is a CV-specific risk for exactly the kind of corpus this project ingests. The ingestion verification step is load-bearing, not ceremonial.

**Concrete action:** Step 1 (corpus ingestion) must include an explicit section extraction verification pass: show the section inventory parsed from each CV file, warn if any section count is below a minimum threshold (e.g. < 4 sections on a 2-page CV is almost certainly a parsing failure), and block ingestion until a human confirms.

---

### R-02 — docx parsing: the Paragraph model needs content-derived fields, not just formatting fields

**From:** RAG LEARNING_NOTES Phase 2 ("The common paragraph model: pluggable earns its keep")

**What was found:** The RAG project added `date` and `override` fields to the `Paragraph` dataclass during the Phase 2 architecture conversation — not at the start. Without them, the docx loader would have lost capability or needed parallel data structures. The architecture conversation that surfaced this was called "the single highest-leverage decision in Phase 2."

**Implication for the orchestrator — CVSection word/line count fields:**
`CVSection.word_count` and `line_count` are exactly this pattern: content-derived fields that must be on the intermediate model, not computed separately later. They are needed for `SectionBudget` derivation (Step 1), for length constraint enforcement in the critique prompt (Phase 3), and for the Phase 5 assembled-length check. If they're not on `CVSection` from ingestion, every downstream component that needs them has to either reparse the file or carry a side-channel.

This validates the schema decision to put `word_count` and `line_count` directly on `CVSection` rather than computing them at critique time.

---

### R-03 — ChromaDB: distance metric is set at collection creation and is immutable

**From:** RFI LEARNING_NOTES entry 9 ("Four collections, one per strategy × distance metric")

**What was found:** `collection.get_or_create()` with `metadata={"hnsw:space": "cosine"}` sets the metric permanently. You cannot switch metric at query time — if you create a collection with L2 and query it expecting cosine, you get wrong rankings with no error.

**Implication for the orchestrator — collection naming discipline:**
The orchestrator creates one ChromaDB collection for the CV section corpus. The collection must be created with the metric explicit in the code and in the collection name (`cv_sections_cosine`). If the collection already exists with a different metric (from an earlier failed setup), `get_or_create` will silently use the old metric. The ingestion step must check that the existing collection's metric matches the configured one, or delete and recreate.

**Concrete action:** In `corpus/ingest.py`, after `get_or_create`, verify `collection.metadata["hnsw:space"] == config.metric`. If it doesn't match, raise with a clear message: "Collection exists with metric X, config requires Y. Run with --replace to recreate."

---

### R-04 — ChromaDB: metadata cannot contain None or empty strings

**From:** RFI LEARNING_NOTES entry 9 ("Metadata sanitisation")

**What was found:** ChromaDB doesn't accept None or empty strings in metadata in some versions. The RFI project stripped them before `collection.add()`. The semantic loss ("this section has no target_company") is preserved by the absence of the key rather than a sentinel value — filtered retrieval still works for sections that do have a target_company.

**Implication for the orchestrator:**
`CVMetadata.target_company` is `str | None`. Before adding any section document to ChromaDB, the ingester must sanitise metadata: omit None-valued keys rather than passing None. Same for any `str` field that might be empty. Add a `sanitise_metadata(d: dict) -> dict` helper to `corpus/ingest.py` that strips None and empty-string values.

---

### R-05 — call_with_retry() is not optional: the embedding API will rate-limit on a real corpus

**From:** RFI LEARNING_NOTES entry 9 ("Four 429 rate-limits during the Strategy B L2 collection; per-collection checkpoint would have lost all progress for that collection")

**What was found:** The RFI project hit 4 rate-limit errors mid-ingestion on a 544-chunk corpus. `call_with_retry()` absorbed them invisibly. Without it, the run would have crashed at chunk ~300 and required full re-ingestion.

**Implication for the orchestrator:**
The CV corpus is smaller (~50-100 sections across 6 CVs) but the same risk applies. More importantly, the refinement loop makes multiple sequential API calls across three providers. A transient 429 from GPT-4o-mini mid-loop without retry would abort the run and lose all iteration state. `call_with_retry()` must wrap every API call from every provider — this is already in the spec but deserves emphasis: it is not a polish item, it is load-bearing from the first real run.

---

### R-06 — Checkpointing granularity: the unit of resumable work is the natural failure unit

**From:** RFI LEARNING_NOTES entry 9 ("Per-file checkpointing, not per-batch and not per-collection") and RAG LEARNING_NOTES Phase 6 ("A long batch job against a rate-limited API MUST checkpoint")

**What was found:** RAG lost 91 completed API calls because the stress test only checkpointed at the end. RFI chose per-(collection, file) checkpointing — the natural unit of recoverable work — over per-batch (too chatty) or per-collection (too coarse, loses too much on failure).

**Implication for the orchestrator:**
The checkpoint unit for the ingestion step is one (section_id) — write to ChromaDB and save the checkpoint after each section is embedded. For the refinement loop, the checkpoint unit is one complete iteration — all section files for iteration N written to disk before iteration N+1 begins. A crash mid-loop loses at most one iteration of work, not the whole run.

This is already in the spec's checkpoint pattern, but the RFI experience makes the granularity choice concrete: per-section for ingestion, per-iteration for the loop.

---

### R-07 — Semantic retrieval beat hybrid on a small, paraphrase-rich corpus — relevant calibration

**From:** RFI LEARNING_NOTES entry 13 ("Counter to the spec's intuition, hybrid does NOT beat semantic on this corpus")

**What was found:** The RFI eval matrix showed semantic retrieval beating hybrid (BM25 + semantic) on a 280–540 chunk corpus where test questions closely paraphrased corpus content. BM25 occasionally promoted high-token-overlap chunks that weren't topically relevant, and RRF's contribution was small when semantic alone was near-saturated. Likely to reverse on larger or more terminology-heavy corpora.

**Implication for the orchestrator:**
The CV section corpus (~50-100 sections, 6 CVs) is even smaller and more paraphrase-rich than the RFI corpus. Phase 1 (fit assessment) uses Mistral embeddings for retrieval. The spec currently uses pure semantic retrieval — this is almost certainly correct for this corpus size. Do not add BM25 hybrid complexity to the Phase 1 retrieval. If the corpus grows substantially (20+ CVs, 200+ sections), revisit.

Documenting this explicitly: the RAG project's "tune when it earns its keep at scale" finding + the RFI project's empirical confirmation both point the same direction.

---

### R-08 — LLM-as-judge over-scores; actionable signal lives in retrieval metrics and edge cases

**From:** RFI LEARNING_NOTES entry 13 ("LLM-judge over-scores. Real signal lives in retrieval-gap and completeness")

**What was found:** Faithfulness = 5.00 and Relevance = 5.00 across all 36 RFI configurations. The judge was consistently too generous on absolute scoring. The actionable metrics were retrieval-gap rate and completeness, which showed real variation. A judge that allows gradations only at the top cannot discriminate good from very good.

**Implication for the orchestrator — critique scoring:**
GPT-4o-mini's `overall_score` (0–10) in the `Critique` schema is playing a role analogous to the LLM-as-judge in the RFI project. If GPT consistently scores drafts at 8.5+ from iteration 1, the critique score will fail to drive the convergence signal. The critique prompt must define the scoring rubric explicitly with anchors: "a 9 requires X, Y, and Z; a 7 means one section still has a major issue; a 5 means multiple structural problems remain." Without anchors, the score will reflect GPT's training priors, not the actual draft quality relative to this JD.

This is a concrete implication for the Step 5 (critique tool) prompt design — it needs explicit score anchors, not just "rate this CV from 0-10."

---

### R-09 — Validate LLM-produced structured output before it touches anything downstream

**From:** RAG LEARNING_NOTES Phase 2 ("When the LLM won't honour your grammar") and RFI LEARNING_NOTES entry 3–4 ("Validate BEFORE showing to human")

**What was found (RAG):** Mistral produced compound `&&` signals on three consecutive iterations despite explicit "FORBIDDEN" language in the prompt. The no-`eval()` parser that rejected them before `chunker.py` ever saw the config was the load-bearing defence. Validating at config-WRITE time (not chunk time) meant the failure happened while the human was watching, not three steps later.

**What was found (RFI):** The validator runs between LLM output and human review — a broken mapping (e.g. same column assigned two roles) is rejected mechanically before the human sees the proposal. Human attention is reserved for semantic correctness only.

**Implication for the orchestrator — structured outputs from Mistral and GPT:**
Phase 0 (Mistral JD extraction → `JDAnalysis` + `ScoringRubric`) and Phase 3 (GPT critique → `Critique`) both produce structured output that downstream phases depend on. Both must be validated against their schemas before use:
- `JDAnalysis`: required fields present, `required_keywords` non-empty, `seniority_level` in known set
- `Critique`: `overall_score` in 0-10, all `CritiqueItem.severity` values in `{"major", "minor"}`, `section` references valid section_ids

Validation failures should be retried once (the LLM may have hallucinated a field name), then surfaced to the human if the retry also fails. Never let a partially-valid `Critique` enter the accept/reject loop — a missing `severity` field would silently bypass the soft-stop condition.

---

### R-10 — The "discover + persist" pattern applies to section boundaries, not just column mappings

**From:** RFI LEARNING_NOTES entry 2 ("Discover schema, don't assume it") and entry 6 ("Persist discovery output, detect section markers")

**What was found:** The RFI profiler discovers per-file schema (sheet, header row, column mapping) and persists it to a config file. The loader treats the config as ground truth — it never re-runs discovery at load time. Discovery is expensive and human-validated; runtime is deterministic and config-driven.

**Implication for the orchestrator — section boundary discovery:**
The ingestion step's section boundary detection (parsing heading styles from .docx files) is the equivalent of the RFI profiler's column mapping discovery. The result — "this CV has these sections in this order" — must be persisted to the `CVSection` metadata and stored in ChromaDB, not re-derived on each tailoring run. If the section structure of a CV changes (rare, but possible if you add a new section), re-ingest with `--replace`. The runtime (tailoring) path treats ChromaDB metadata as ground truth.

This also means the YAML front-matter written per section file during ingestion is load-bearing — it is the persisted discovery output.

---

### Summary: what changes, what is confirmed, what is new

**Confirmed as-is in the spec:**
- Semantic-only retrieval for Phase 1 (R-07)
- `call_with_retry()` wrapping all API calls (R-05)
- Per-section + per-iteration checkpointing (R-06)
- Schema validation before downstream use (R-09)

**Requires concrete action before Step 1:**
- Ingestion verification pass: section inventory + human confirmation gate (R-01)
- ChromaDB collection metric verification on `get_or_create` (R-03)
- Metadata sanitisation helper (strip None and empty strings) in `corpus/ingest.py` (R-04)

**Requires prompt design decision before Step 5:**
- Critique `overall_score` must have explicit anchors (e.g. 9 = X, 7 = Y, 5 = Z) to avoid the LLM-judge over-scoring problem (R-08)

**Validates schema decisions already made:**
- `CVSection.word_count` and `line_count` as fields on the intermediate model, not computed later (R-02)
- Section discovery persisted to ChromaDB metadata, not re-derived at runtime (R-10): the unit of work is a section, not a CV

**What was decided:**
The refinement loop operates at section granularity. Each section is drafted,
critiqued, revised, and converged independently. Sections that converge early
are frozen — excluded from subsequent critique calls. The final CV is assembled
from the best-accepted version of each section, ordered by `CVSection.position`.

**Alternatives rejected:**
- *Monolithic CV drafting* — treating the CV as a single document means a strong
  profile and a weak experience section share the same iteration count. The loop
  can't stop working on the profile just because it's done.
- *Section-level drafting, CV-level critique* — sending the full CV to GPT for
  critique but revising sections individually. Rejected because critique items
  would still be scoped to sections, making the full-CV send unnecessary tokens.

**Load-bearing reason:**
Different sections converge at different rates. The profile might be excellent
after one pass; a specific experience section might need three. Freezing converged
sections makes each subsequent iteration cheaper and the critique more focused.
By iteration 3, the system is often only critiquing one or two sections.

**The real-world observation that drove this:**
The user sometimes prefers an earlier version of a section over the final one —
e.g. profile v2 might be sharper than profile v3 if the final revision over-optimised.
Section-level storage makes this recoverable: Phase 4 HITL can show section
version history and let the human choose.

**What this generalises to:**
Decomposing a document into independently refinable units is a general pattern
for any iterative generation task with heterogeneous quality across parts. The
unit of convergence should match the unit of quality variance — not the unit of
output format.

**Interview framing:**
"The refinement loop works at section granularity, not whole-CV. Sections that
converge early get frozen, which makes each subsequent iteration cheaper and
more focused. The final CV is assembled from the best version of each section —
and because intermediate versions are checkpointed, the human can choose an
earlier version of any section if a later revision over-optimised."

---

### D-13 — Static sections: presence in assembly, invisible to the loop

**What was decided:**
`CVSection.static: bool` marks sections that are copied verbatim from the base CV
and never sent to the critique loop. `interests` is always static. `education`
and `certifications` are typically static. Static sections appear in the
assembled CV at their correct `position` but generate no `CritiqueItem` entries,
no `SectionScore` entries, and no version files — only a single `_static.md`
checkpoint.

**Load-bearing reason:**
Including static sections in the critique loop wastes GPT tokens on content
that won't change and can't meaningfully improve. Marking them explicitly also
makes the HITL display cleaner — the human can see at a glance which sections
were worked on vs. carried over unchanged.

**Interesting edge case:**
`interests` is not just static — it's a proof that the `static` flag is about
editorial intent, not section importance. Interests won't change between
tailoring runs for the same person, but it needs to appear in the right position
in the assembled CV. Static ≠ absent.

---

### D-14 — Length budget derived at ingestion from observed corpus behaviour

**What was decided:**
Section length constraints are not hardcoded. After ingesting all CVs, the
ingestion script computes a `SectionBudget` per `section_type` from observed
word counts: `min_words`, `max_words`, `target_words` (median). Written to
`budgets.yaml`. The total word count across all section targets is the
two-page envelope, derived from the user's actual CV corpus.

The critique prompt uses `target_words` as the drafting target and flags:
- `major` if a section exceeds `max_words` (breaks the two-page constraint)
- `minor` if a section is materially below `min_words` (undertells the role)

Phase 5 (Haiku) does a final assembled-length check before output.

**Alternatives rejected:**
- *Hardcoded word limits per section* — requires the user to estimate limits
  before seeing how the system behaves. Error-prone and not portable.
- *No length constraint* — the two-page constraint is real and non-negotiable.
  Without it, the system would produce excellent-but-unsubmittable CVs.

**Load-bearing reason:**
The best source of truth for "how long should this section be" is the user's
own existing CVs. They've already made these tradeoffs when writing them.
Measuring from the corpus respects those decisions rather than imposing external
constraints.

**Portfolio/adapting note:**
Documented in `ADAPTING.md`: if you are adapting this project for your own CV
corpus, run ingestion first to derive budgets before any tailoring runs. The
`budgets.yaml` output shows you exactly what the system inferred about your
section lengths.

**What this teaches:**
Constraints that matter to the user (two-page CV) should be captured as
measurable invariants and enforced throughout the loop — not just checked at
the end. Making the constraint a `CritiqueItem` means it competes on equal
footing with content improvements: the orchestrator can decide to prioritise
length compliance over a marginal wording improvement.

---

### D-15 — CVs are .docx files; ingestion uses python-docx with heading-style parsing

> **⚠ CORRECTED during Step 1 — see F-04/F-05.** The real corpus has no reliable
> heading-style structure (table-based; section headers span Heading 1/3/4 and
> 16/14pt; companies collide with section headers at 14pt). Section detection is
> now **canonical-name vocabulary matching + a size-based company split inside
> the experience block** (D-19), using the reused RAG table-aware `docx_loader`.
> The heading-style assumption below is retained for the record but superseded.

**What was decided:**
All source CVs are `.docx` files. The ingestion parser uses `python-docx` to
extract section text and measure word/line counts. Section boundary detection
uses heading styles (Heading 1 / Heading 2), not line splitting or regex
patterns. YAML front-matter is written to companion `.yaml` files alongside
the extracted section markdown files.

**Why heading-style parsing, not line splitting:**
Learned from the Week 1 RAG project: `.docx` files with consistent heading
styles parse cleanly with `python-docx`; files that use manual formatting
(bold text, font size changes) instead of styles require fallback heuristics
that are fragile. The CV corpus uses consistent heading styles — this is a
precondition for ingestion to work reliably, and should be documented in
`ADAPTING.md`.

**Adapting note:**
If adapting this project for your own CV corpus: ensure your .docx files use
Word heading styles (Heading 1 for section titles) rather than manually
formatted text. The ingestion parser will fail silently on manually formatted
headings — verify section extraction output before running tailoring.

**What this generalises to:**
Document parsing quality is a function of document authoring discipline.
Any system that ingests structured documents should specify the authoring
conventions it depends on, and verify them at ingestion time rather than
assuming them.

---

### D-16 — Fit assessment has three outcomes; no_fit stops the pipeline

**What was decided:**
Phase 1 produces one of three outcomes: `strong`, `partial`, or `no_fit`.
`no_fit` terminates the pipeline immediately — no drafting, no API spend
beyond Phase 1. The `no_fit_reason` field provides a plain-English explanation.

Gap types and their pipeline implications:
- `keyword` — fixable by tailoring; never triggers no_fit
- `experience` — partially addressable; triggers partial at worst
- `hard_requirement` — not fixable (missing credential, clearance, certification); triggers no_fit
- `seniority` — judgment call; triggers no_fit only on severe mismatch

The human can override no_fit and proceed anyway — the system is honest, not gatekeeping.

**Load-bearing reason:**
A fit assessment that always proceeds is less valuable than one that can say
"don't apply." The most useful output of the pipeline is sometimes "this JD
has a non-negotiable requirement you don't meet." Saving token spend and
application time is a feature. Making the stop path explicit (typed outcome,
plain-English reason, override option) is better than letting the pipeline
produce a confidently tailored CV for a role the candidate can't fill.

**What this teaches:**
Pipeline early-exit is a first-class outcome, not an error state. Any system
that processes input should have an explicit "this input is out of scope"
path. Explicit is better than letting a downstream stage produce wrong output.

**Interview framing:**
"The fit assessment can stop the pipeline entirely if it finds a blocking gap.
That saves time and API spend, and it's honest. The human can override if they
want to apply for a stretch role, but the system won't pretend a gap doesn't exist."

---

### D-17 — Phase 1 recommends a section-level mix, not a single base CV

**What was decided:**
`FitAssessment.recommended_sections` is `dict[str, SectionRecommendation]` —
the best source section for each section_type drawn from across the full CV
corpus. `FitGap` replaces the flat `skills_gaps: list[str]` with a typed
structure: `gap_type`, `addressable`, `severity`, `reason`.

**Load-bearing reason:**
With section-granular ingestion, different CVs may have the strongest version
of different sections. Recommending a single CV from a section-level corpus
ignores retrieval capability that's already built. The recommendation granularity
should match the retrieval granularity.

**The HITL consequence:**
The Phase 1 checkpoint shows a section mix table. The human can override
individual section sources. This is more useful than yes/no on a single CV,
and the conversational HITL (D-18) handles the override naturally.

**What this teaches:**
The retrieval granularity and recommendation granularity should always match.
If you've built section-level retrieval, the recommendation should be at section
level. Anything coarser leaves retrieved capability unused.

---

### D-18 — Conversational HITL: natural language interpreted into structured pipeline decisions

**What was decided:**
Each HITL checkpoint is a conversational exchange with explicit options plus
a free-text escape hatch. A Claude Haiku call interprets free-text responses
into structured decisions. The interpretation is shown back to the human for
confirmation before the pipeline resumes.

Three HITL checkpoints, three interaction patterns:
- **Phase 1 (fit assessment):** conversational + free text; Haiku interprets
- **Phase 4 (section review):** lettered options + free text [e]; Haiku interprets;
  Sonnet executes revision if needed
- **Phase 5 (formatting):** binary only — Approve / Reject; no Haiku needed

**The escape hatch is load-bearing:**
Offering only lettered options makes the HITL a fancy button set. Option [e]
makes it genuinely conversational — "make the Barclays section more concise
and drop the team size mention entirely" — and the system handles it.

**The Haiku interpretation pattern:**
Haiku receives checkpoint context + human free-text, returns a structured
decision object. Small, bounded, cheap. The output is confirmed back to the
human before the pipeline resumes — the human always knows what the system
understood.

**What this teaches:**
Natural language is a better HITL interface than constrained options alone,
but only if free-text is interpreted into structured decisions before touching
the pipeline. The interpretation layer (Haiku) is what makes free-text safe
to act on. Generalises to any system where human input needs to be expressive
but downstream processing needs structured input.

**Interview framing:**
"The HITL checkpoints are conversational. A small Haiku call interprets free
text into a structured instruction before it touches the pipeline, and shows
the interpretation back to the human for confirmation. The pipeline gets
structured input; the human gets an expressive interface."

---

### D-28 — Dual-writer refinement loop: two independent drafters, one orchestrator

**What was decided (mid-build, after Step 4 validated the single-writer pipeline):**
The refinement loop uses two independent writers — Claude Sonnet and GPT-4o-mini —
each producing their own draft of every active section per iteration. The Claude
Sonnet orchestrator (in a separate, explicit role) adjudicates: scores both drafts,
selects or synthesises the best text, and sets direction for the next iteration.
Both writers can push back on the orchestrator's direction with explicit reasoning
(one exchange only). The orchestrator reads pushbacks and may revise its direction
before the next iteration begins.

**Why this replaces the prior design (single writer + GPT critique):**
The single-writer design had GPT-4o-mini returning `CritiqueItem` lists (issues +
suggestions), with Claude revising against accepted items. The limitation: two
writers with different priors produce a richer output space than one writer with
one critic. GPT's suggested draft is more useful input to Claude than a list of
issues — "here's what I'd write" forces engagement rather than pick-and-choose
from suggestions. This mirrors the manual workflow (write with Claude, rewrite with
ChatGPT, push back to Claude) that produced the best real results.

**D-03 is preserved:** GPT is still the challenger, Claude still the primary
writer and orchestrator. Roles differ (both write now) but the model-selection
rationale — GPT as harsher, more direct — remains valid. GPT's drafts serve the
same function as its critiques did before.

**Cost accepted:** Initial estimate ~$2–4 per full-mode run. **F-28 corrected this
to ~$0.79** (Airwallex JD, Sonnet + GPT, 8 active sections, converged at iter 2).
The estimate was conservative by 3–5×: the loop converged before the cap, and
section freezing meant later iterations were cheap. The quality uplift is real
(F-21, F-28) and the cost is well within the $5 full-mode cap.

**Schema changes:**
- `WriterDraft` added: `{writer, section_id, text, version, pushback}`
- `OrchestratorDecision` added: `{section_id, selected_base, direction, synthesis_notes, claude_quality, gpt_quality, keyword_coverage, converged, rubric_additions}`
- `CritiqueItem.source_writer` added (which writer raised the item)
- `SectionScore` gains `claude_quality`, `gpt_quality`, `selected_writer` (replaces `critique_score`)
- `IterationScore.critique_delta` renamed to `quality_delta`
- `Critique` class removed — writers self-assess and flag issues within their drafts
- `tools/critique.py` replaced by `tools/claude_writer.py`, `tools/gpt_writer.py`, `tools/orchestrator_tool.py`

**What this teaches:**
Multi-writer orchestration is a natural extension of the tool pattern. The
orchestrator's role becomes more valuable when it has two drafts to arbitrate rather
than one draft to accept/reject suggestions on. "Editor with two manuscripts" is a
richer and more defensible design than "author with a punch list."

**Interview framing:**
"The loop has two independent writers — Claude and GPT — each drafting every
section separately. The orchestrator compares both, scores them, and can synthesise
the best of each. Both writers can push back on the orchestrator's direction with
explicit reasoning. It mirrors the manual workflow I found most effective."

---

### D-29 — Writer pushback: one exchange, structured, logged

**What was decided:**
After the orchestrator issues a decision and direction, both writers get one
opportunity to push back with explicit reasoning (`WriterDraft.pushback: str | None`).
The orchestrator reads both pushbacks and decides whether to revise direction or
hold. One exchange only — the pushback is not subject to further pushback.

**Purpose:** surfaces genuine model disagreement before it silently accumulates as
quality drift across iterations. If Claude thinks the direction would weaken a
section, it says so. The orchestrator adjudicates; the reasoning is logged.

**What this is not:** a negotiation loop. One exchange prevents the pushback
mechanism from burning tokens without converging.

---

### D-30 — Loop-level memory: structured state forwarded; prose reasoning stays in audit trail

**What was decided:**
`LoopMemory` forwarded each iteration: `rejected_suggestions` (accumulating),
`orchestrator_directions` (one per iteration), `frozen_sections`, `iteration_scores`.

This is signal, not prose reasoning. D-06 preserved: verbose orchestrator reasoning
stays in `run_log.jsonl` only. What flows forward is structured state — a shared
whiteboard, not a conversation history.

**The failure mode prevented:** without `rejected_suggestions`, GPT re-raises
the same rejected item every iteration, burning a writing slot and forcing
re-litigation of a settled point. In a 3-iteration loop this can consume a full
iteration on re-discussion.

---

### D-31 — Prompt caching on stable blocks (post-prompt-tuning)

**What was decided:**
Anthropic `cache_control` breakpoints on stable blocks (system prompts, JD
requirements, rubric) once prompts are stable. Variable content (current drafts,
direction, loop memory) appended after cached prefix. Cache breakpoints set
**after** prompt tuning — caching a prompt under active development wastes cache
fills.

**Caveat:** rubric updates mid-loop invalidate the rubric cache block. Rubric
placed after system prompt but before per-section variable content — a rubric
update invalidates one cache level, not the system prompt cache.

**Outcome (F-22):** implemented and measured. Wiring is correct (a 4202-token
control caches and reads back 100%), but the real stable prefix is ~534 tokens —
under both provider minimums — so it's a **no-op at this prompt scale**, on Sonnet
as well as Haiku. Kept because it's costless and scales automatically; not worth
padding prompts to force, since the cacheable bulk is small.

---

### D-32 — Experience role/date lines are structural facts; split at Phase 2, re-attach verbatim at Phase 6

**What was decided (from F-29):**
Experience section role/date lines (`Senior Product Manager (Apr 2022 – Mar 2024)`)
are never entered into the draftable text. `phase2._split_role_line` peels the
leading non-bullet line(s) off each experience section before the draft call.
The LLM rewrites only the bulleted body. The verbatim role line is stored in
`manifest[sid]["role_line"]` and re-attached bold between the company heading
and body at assembly (`phase6.assemble_markdown`). Writers in Phase 3 only ever
see the body — they cannot drop the role line either.

**Why this failed without the fix:**
The Phase 2 drafter was told "output ONLY the section text, no heading." The LLM
treated the first line of the section (the role/date line) as a heading and dropped
it inconsistently — Microsoft lost it, Utiq kept it (F-29). Because the CV heading
is the company name alone (F-23), a dropped role line leaves the section with no
visible job title. Promotion stacks (D-21) made this worse: two role-groups at one
employer collapsed to two identical `## Appnexus / Xandr` blocks with no way to
distinguish them.

**Principle:** The same as D-13 (static sections) applied at sub-section level:
deterministic where the content is a fact, LLM only for judgment and wording.
Job titles and dates are facts — they must survive verbatim. "Ask the model
nicely to keep them" is not a guarantee. Storing them outside the draftable text
and re-attaching them at assembly is the guarantee.

**Promotion stacks (D-21):** Multiple role lines before shared bullets are
captured as a multi-line `role_line` and each rendered bold — all titles in a
promotion stack are preserved.

**What this teaches:** Every structured document generation system has content
that is factual/structural (preserve verbatim) and content that benefits from
model judgment (rephrase/reweight). The boundary should be enforced
architecturally, not left to prompt instructions. Prompt instructions drift;
structural separation doesn't.

**Interview framing:**
"Job titles and dates in a CV are facts — they must appear exactly as written.
Rather than instructing the model to preserve them, I split them out before the
draft call and re-attach them verbatim at assembly. Same principle as static
sections: deterministic where the content is a fact, model judgment only where
it adds value."

**Affects:** F-29, F-23, D-21, D-13, Phase 2 manifest contract, SPEC §5 Phase 6.

---

### D-33 — CVCM: candidate value creation model as optional context artifact

**What was decided:**
A markdown file (`candidate/value_creation_model.md`) authored and maintained
by the candidate is consumed as optional context at Phases 1, 2, and 3. The
system never generates or modifies it. The pipeline runs normally without it —
no degradation to structural fit scoring. Its presence shifts tailoring from
pure keyword optimisation toward articulation of authentic candidate value.

**What the CVCM captures:**
Recurring patterns that explain why organisations hire, trust, promote, and
retain the candidate — independent of job titles, industries, or specific
achievements. Problem-solving approach, leadership philosophy, stakeholder
engagement style, value creation mechanisms, recurring career themes.

**Phase integration:**
- Phase 1: loaded alongside JDAnalysis; fit assessment gains a fourth dimension
  (value creation alignment); `FitAssessment.value_alignment_notes` populated.
- Phase 2: passed to drafting prompt; writers instructed to frame experience
  through value creation patterns, not just keyword alignment.
- Phase 3: passed to both writers and orchestrator. When draft quality scores
  are within 1.0 (the existing tiebreak band, D-28), CVCM is used as secondary
  selection factor — preference to draft better articulating authentic value.

**Alternatives rejected:**
- *System-generated CVCM* — generation from CVs + reflections is a separate
  pipeline that doesn't exist yet. Generation is explicitly out of scope for
  this build; noted as future work.
- *CVCM as primary convergence signal* — keyword coverage and quality scores
  remain primary. CVCM is a qualitative overlay and tiebreaker, not a
  replacement for structured scoring.
- *Required CVCM* — optional is correct. A strong CV corpus + rubric produces
  good tailoring without it.

**What this teaches:**
Durable candidate artifacts (persist and improve across applications) are
qualitatively different from run-specific inputs (JD, CV corpus). The
architecture should distinguish them: run-specific inputs drive the structured
scoring loop; durable artifacts provide qualitative context that improves
output authenticity without changing convergence mechanics.

**Interview framing:**
"The system has two kinds of inputs: run-specific (the JD, the CV corpus) and
durable candidate context (the value creation model). The keyword scoring and
convergence signals use the run-specific inputs. The CVCM shifts the framing
of what gets written — from keyword optimisation to articulation of authentic
value. It's optional, but when present it's the difference between a CV that
scores well and a CV that feels like you."
