# CLAUDE.md — tailor/tools/

LLMs-as-tools and the deterministic scorer. The loop calls these exactly as the
Week 2 agent called SQL queries (D-02). Read `tailor/CLAUDE.md` first.

## The tool contract

- **Each tool returns a typed `models.py` object; the provider is hidden.** Tools
  call `helpers.claude_complete` / `gpt_complete` / `embed_*`, never an SDK
  directly, so `call_with_retry` wraps every call (R-05) and the loop never learns
  which provider ran (D-02).
- **Validate structured output before returning it; retry once, then raise** (R-09).
- **Prefer "leave state unchanged" over "corrupt state" on failure** (`rubric.py`
  returns the rubric untouched if the model call fails — a version bump always
  means a real change).
- **Tolerate a missing optional array.** Anthropic does NOT hard-enforce a tool's
  `required` like OpenAI strict mode — Haiku may omit `items`. Read it as
  `data.get("items") or []`, never `data["items"]` (F-20).

## The dual-writer loop tools (D-28)

Two independent writers + one orchestrator replace the old single critique tool.
`writer_common.py` holds what must stay calibrated between the two writers:
truthfulness rules (`TRUTHFULNESS_RULES`), structure-preservation rules
(`STRUCTURE_RULES`), the two severity definitions (D-11), the source-anchored word
target (D-27/F-13), and the deterministic length-budget items (D-14) — applied to
**both** writers' drafts (F-17), code counts words, the model judges content.

- **Structure preservation is a deterministic gate, not a prompt hope (F-56).** Four layers,
  weakest-model-proof:
  1. `STRUCTURE_RULES` — a top-level block placed BEFORE the content guidance in both writer
     prompts (and the Phase-2 prompt): match the source's list shape (bulleted experience stays
     bullets, a `·`-delimited skills list stays a list).
  2. `structure_preserved(source, draft)` — **counts list markers in code** (never the model's
     self-report); the writer stamps it on `WriterDraft.structure_preserved`.
  3. The orchestrator disqualifies a `structure_preserved=False` draft (see below).
  4. `enforce_source_structure(source, text)` — the **backstop**: when a bulleted source was
     flattened to prose and NO draft kept it (common on Haiku/demo, where both writers flatten
     and a single iteration never recovers), it splits the prose back into bullets on sentence
     boundaries. **Pure reformatting — inserts only `- ` + newlines, changes no words**, so it's
     truthfulness-safe. Applied at both persist points (Phase 2 v0, Phase 3 selected text); a
     no-op in full mode where the writers keep structure. A `·`-skills list flattened to prose
     is NOT deterministically reconstructable, so that case relies on layers 1–3.
  Don't trust a prompt alone for what a deterministic check can guarantee (cf. the F-38 keyword
  Goodhart fix).

- **`claude_writer.py`** — Claude as the precise, evidence-led writer. Forced
  `submit_draft` tool → `{text, items}`; `pushback()` → `str | None` (D-29).
- **`gpt_writer.py`** — GPT-4o-mini as the harsher, bolder writer (D-03). Same
  interface; OpenAI strict `json_schema` enforces the severity enum server-side.
- **`orchestrator_tool.py`** — Claude as the editor with two manuscripts. Scores
  both (0–10, explicit anchors so it can't over-score — R-08/F-14), selects or
  **synthesises**, sets `direction`, judges `converged`. It is given the **SOURCE
  section** (`source_text`, the text the writers tailored from) and judges
  truthfulness FIRST — fabrication (invented title/sector/identity, or an unsupported
  JD keyword) caps a draft at 4/10 and blocks `converged` (F-34). It also sees each draft's
  `structure_preserved` flag and treats **False as a selection disqualifier** (a draft
  flattened to prose can't be the base; both-flattened forces `converged=False` with a
  restore-format direction, F-56). The source rides in
  the user message, not the cache prefix (it varies per section, D-31). `adjudicate()` returns
  `(OrchestratorDecision, selected_text)` — the decision is a summary (no draft
  text; drafts live on disk, D-07 #3), `selected_text` is what the loop
  checkpoints. Pure claude/gpt picks use the chosen draft verbatim (no rewrite
  drift); synthesis returns the orchestrator-merged text (F-18). `keyword_coverage`
  is computed in code by the scorer (D-25), not asked of the model, and is
  **source-grounded** (F-38: only source-supported keywords count). Proposed
  `rubric_additions` are raw — the loop JD-validates + caps them via `rubric.py`.

## verifier.py — the fabrication gate (F-35)

- After refinement, `verify_run` checks each non-static section's final text against its
  **raw corpus source** (`sections/<id>_source.md`, persisted by Phase 2) and returns
  `CritiqueItem`s for unsupported claims. They flow into Phase-4 review, the audit log, the
  report's Grounding tab, the summary `fabrication_flags`, and a CLI warning — never silent.
- **Precision is the whole game (F-35).** Flag ONLY new checkable facts (metric, employer,
  title, sector, named system) with no source basis; rewording / paraphrase / dropped
  qualifiers are SUPPORTED. A "list unsupported claims" prompt over-flags badly on Haiku
  (9 FPs → 1 real after the precision rewrite). Keep the "find the supporting span first;
  when unsure, don't flag" framing. Safety net: a malformed check returns no findings, never crashes.

## rubric.py — dynamic rubric updates (D-04, unchanged across the dual-writer rewrite)

- Orchestrator-proposed additions are **validated against the JD** ("implied or
  hallucinated?"), capped at `max_additions_per_iteration`, tracked as
  `RubricAddition` with provenance. De-dup before the model call.

## scorer.py — keyword coverage (deterministic, no API)

- **Token-subset matching, not exact-phrase** (D-25/F-10). `keyword_coverage` is
  per-section; `union_coverage` is CV-level (fraction covered anywhere) — the
  aggregate the loop uses for `IterationScore.keyword_coverage` (F-15).
- **Supported coverage = the Goodhart fix (F-38).** Pass `source_text`/`source_texts`
  and a keyword counts only when present in the draft AND evidenced by the source; an
  inserted-but-unsupported keyword earns no coverage (denominator stays the full pool,
  so the score is monotone-honest — surfacing real strength raises it, inventing one
  does not). **No source arg → draft-only scoring, unchanged** — Phase 1 scores the
  raw corpus (text == source) so it passes none; Phase 3 passes each section's raw
  source. `adjudicate` maps an absent source (`""`) to draft-only, never zeroing.
