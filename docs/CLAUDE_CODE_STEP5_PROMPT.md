# Claude Code Resumption Prompt — cv-tailor Step 5
## Picking up after Step 4 (Phase 2 initial draft complete)

Paste this into the active Claude Code session. Read both updated documents
before writing any code.

---

## Where we are

Steps 0–4 are complete and validated on real data:

| Step | What was built | Key findings |
|------|---------------|-------------|
| 0 | Schemas + audit logger + Docker | F-01, F-02, F-03 — package is `tailor/`, Python 3.13, 49 tests pass |
| 1 | Corpus ingestion + ChromaDB | F-04 (table-based CVs, not heading-styled), F-05 (vocabulary+size parser), F-06 (sidecar scalars), F-07 (mistralai 2.4.9 import path) |
| 2 | Phase 0 JD analysis + scorer | F-08 (cost estimates), F-09 (mistral-small + prompt fix), F-10 (token-subset keyword matching), D-24, D-25 |
| 3 | Phase 1 fit assessment | F-11 (section mixing has real value), F-12 (Phase 1 validated on Airwallex + JPMC), D-23 (soft seniority filter) |
| 4 | Phase 2 initial draft | F-13 (draft target = clamp to source length, not median), D-27 |

Phase 2 produces `outputs/<run_id>/sections/<section_id>_v0.md` for each
non-static section, and `<section_id>_static.md` for static sections.

Convergence thresholds confirmed on real data (F-16, run ahead of Step 6):
`keyword_delta < 0.05`, `quality_delta < 0.5` — no change needed.

---

## Architecture change: dual-writer loop (read this before touching any code)

**The Phase 3 design has changed significantly from what was in the spec before
Step 4.** Read the updated `SPEC_ORCHESTRATOR.md` Phase 3 section and §4 schemas
in full. Here is the summary:

**Old design:** GPT-4o-mini critiques Claude's draft → Claude revises accepted items.
`tools/critique.py` was the sole tool.

**New design:** Both Claude Sonnet and GPT-4o-mini write independent drafts of
every active section. A Claude Sonnet orchestrator (in a separate, explicit role)
adjudicates — scores both drafts, selects or synthesises the best, sets direction
for the next iteration. Both writers can push back on the direction (one exchange
only). Loop memory (rejected suggestions, directions, frozen sections) forwarded
each iteration as structured state.

**Why:** mirrors the manual workflow (write with Claude → rewrite with GPT →
push back to Claude → iterate) that produced the best real results. Two drafts
give the orchestrator a richer output space than one draft + a punch list.

**Schema changes you need to know:**
- `WriterDraft` is new: `{writer, section_id, text, version, pushback: str|None}`
- `OrchestratorDecision` is new: `{section_id, selected_base, direction, synthesis_notes, claude_quality, gpt_quality, keyword_coverage, converged, rubric_additions}`
- `SectionScore` now has `claude_quality`, `gpt_quality`, `selected_writer` (not `critique_score`)
- `IterationScore.critique_delta` is now `quality_delta`
- `CritiqueItem.source_writer` added (which writer raised the item)
- `Critique` class is gone — writers self-assess and include critique items in their output
- `tools/critique.py` does not exist — build `tools/claude_writer.py`, `tools/gpt_writer.py`, `tools/orchestrator_tool.py`

All three new schemas are in `models.py` and in the updated SPEC. The `Serializable`
mixin (F-02) handles round-trips — add the new dataclasses to `test_schemas.py`
first (49 → N tests) before building the tools.

---

## Step 5 — Build the three writer/orchestrator tools in isolation

Build and test each tool independently before wiring any of them into the loop.

### 5a — Update `models.py` and tests

Add `WriterDraft`, `OrchestratorDecision` to `models.py`. Update `SectionScore`
and `IterationScore` to match updated schemas. Remove `Critique` class (no longer
used). Add round-trip tests for new schemas to `test_schemas.py`.

```bash
docker compose run --rm cli pytest tests/test_schemas.py -v
```

All tests must pass before building any tool.

### 5b — Build `tools/claude_writer.py`

Interface:
```python
def write_section(
    section_id: str,
    section_text: str,          # current text (source or prior iteration's selected)
    jd: JDAnalysis,
    rubric: ScoringRubric,
    budget: SectionBudget,
    direction: str | None,      # None on first iteration
    rejected_suggestions: list[str],  # accumulates across iterations
    is_final: bool,
    config: RunConfig,
) -> WriterDraft
```

Prompt design requirements:
- Word target = `clamp(len(section_text.split()), budget.min_words, budget.max_words)`
  per D-27/F-13 — anchored to source length, not corpus median
- Severity definitions for self-assessment: major = materially weakens application
  or contradicts JD; minor = improvement opportunity
- If `direction` is not None: include it explicitly as "The orchestrator's direction
  for this section: [direction]"
- If `rejected_suggestions`: "Do not re-raise these — already considered: [list]"
- If `is_final`: "This is the final pass — produce your definitive version"
- Returns `WriterDraft` with `pushback: str | None` (None on first call)
- Schema-validated; retry once on failure

### 5c — Build `tools/gpt_writer.py`

Same interface as `claude_writer.py`. Key implementation differences:
- Uses OpenAI strict `json_schema` for structured output (severity enum enforced
  server-side — prevents a bad severity reaching the soft-stop logic)
- `section_scores` returned as a list (strict mode needs `additionalProperties:false`);
  convert to dict after parsing
- Length-budget violations appended deterministically in code (word count), not
  left to GPT
- Score anchors in prompt: "9–10 = publication-ready; 7–8 = one minor gap remains;
  5–6 = multiple structural issues; 3–4 = weak draft" (F-14 confirmed these
  discriminate — weak → 3.0, strong → 8.0)

### 5d — Build `tools/orchestrator_tool.py`

Interface:
```python
def adjudicate(
    section_id: str,
    claude_draft: WriterDraft,
    gpt_draft: WriterDraft,
    rubric: ScoringRubric,
    jd: JDAnalysis,
    prior_scores: SectionScore | None,
) -> OrchestratorDecision
```

Prompt design:
- Same score anchors as gpt_writer (scores must be on the same scale)
- Produces `selected_base` ("claude"|"gpt"|"synthesis"), `synthesis_notes` (if
  synthesis: what to take from each), `direction` (what both writers should focus
  on next), `claude_quality`, `gpt_quality`, `keyword_coverage`, `converged`,
  `rubric_additions` (max 2, must be JD-validated)
- Schema-validated; retry once

### 5e — Isolation tests

Test each tool with a real section + real JD (use the Airwallex JD and one of
the v0 section files from a prior Phase 2 run). Do not mock — test against live
APIs at this stage.

```bash
docker compose run --rm cli python -c "
from tailor.tools.claude_writer import write_section
from tailor.tools.gpt_writer import write_section as gpt_write
from tailor.tools.orchestrator_tool import adjudicate
# ... test with real section_id, section_text, jd, rubric
"
```

*Verification gates before proceeding to Step 6:*
- [ ] Given identical section + two meaningfully different drafts, orchestrator
  selects the better one with a coherent direction
- [ ] `claude_quality` ≠ `gpt_quality` (scores discriminate)
- [ ] Weak draft → major CritiqueItem; strong draft → minor or none
- [ ] `WriterDraft.pushback` is a string when the writer disagrees; None otherwise
- [ ] All three tools schema-validated and retry on failure
- [ ] `test_schemas.py` passes with new dataclasses added

---

## Step 6 — Refinement loop (dual-writer, section-granular)

Build `phases/phase3_refinement.py` and `tools/rubric.py` once Step 5 gates pass.

### Loop structure per iteration

```python
# For each active (non-frozen) section:
# 1. Dual write
claude_draft = claude_writer.write_section(...)
gpt_draft = gpt_writer.write_section(...)

# 2. Orchestrator adjudication
decision = orchestrator_tool.adjudicate(section_id, claude_draft, gpt_draft, ...)

# 3. Write selected text to disk
write_section_file(section_id, decision, run_id)

# 4. Pushback exchange (if not final iteration)
claude_pushback = claude_writer.pushback(decision, ...)
gpt_pushback = gpt_writer.pushback(decision, ...)
revised_direction = orchestrator_tool.read_pushbacks(claude_pushback, gpt_pushback, decision)

# 5. Score + freeze
section_score = compute_section_score(decision)
if decision.converged: frozen_sections.add(section_id)

# 6. Log
audit.log(ReasoningEntry(...))
```

### LoopMemory — forward this each iteration

```python
@dataclass
class LoopMemory:
    rejected_suggestions: list[str]     # accumulates — prevents re-litigation
    orchestrator_directions: list[str]  # one per completed iteration
    frozen_sections: list[str]
    iteration_scores: list[IterationScore]
```

### Build order within Step 6

1. `tools/rubric.py` — rubric update + JD-validation logic. Test in isolation first.
2. `phases/phase3_refinement.py` with `max_iterations=1` — validate single-pass
   mechanics (dual write → adjudication → disk write → pushback → scoring → freeze)
3. Multi-iteration run — verify `quality_delta` decreasing, frozen sections excluded,
   `rejected_suggestions` forwarded, `LoopMemory.orchestrator_directions` grows

### Prompt caching

Add `cache_control` breakpoints **after prompts are stable** (not during tuning):
- Breakpoint 1: end of system prompt
- Breakpoint 2: end of JD requirements + rubric block
Variable content (current drafts, direction, loop memory) appended after.
Rubric block after system prompt but before per-section content — rubric update
invalidates one level, not the system prompt cache.

*Verification gates for Step 6:*
- [ ] Single-iteration: valid `OrchestratorDecision` per section; disk files written
- [ ] Multi-iteration: `quality_delta` decreasing or plateauing; convergence fires
  at expected iteration; convergence thresholds unchanged (F-16)
- [ ] Frozen sections excluded from iteration 2 dual write + adjudication
- [ ] `LoopMemory.rejected_suggestions` accumulates correctly
- [ ] `ReasoningEntry` logged for every decision including freeze events
- [ ] run_log.jsonl complete and readable after a full demo-mode run

---

## Working rules (unchanged)

- **Read updated SPEC and LEARNING_NOTES before writing any code.** Both have
  been updated to reflect the dual-writer design.
- **No code before the decision is clear.** If Step 5 reveals an ambiguity,
  stop and flag it.
- **Every finding goes in LEARNING_NOTES** as F-17, F-18, etc. Same format.
- **Test tools in isolation before wiring.** The loop is complex — a broken tool
  discovered mid-loop costs more than an isolation test.
- **Demo mode (Haiku orchestrator) for all dev runs** per D-26. Final Sonnet
  validation after Step 6 is complete.
- **call_with_retry() wraps every API call** from all three providers.

---

## Files to copy into the repo before starting

Replace these files in the repo with the updated versions from this conversation:
- `SPEC_ORCHESTRATOR.md` — Phase 3 rewritten, schemas updated
- `LEARNING_NOTES_ORCHESTRATOR.md` — D-28 through D-31 added

Both files are attached to this conversation as downloads.
