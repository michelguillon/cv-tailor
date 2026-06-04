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
truthfulness rules, the two severity definitions (D-11), the source-anchored word
target (D-27/F-13), and the deterministic length-budget items (D-14) — applied to
**both** writers' drafts (F-17), code counts words, the model judges content.

- **`claude_writer.py`** — Claude as the precise, evidence-led writer. Forced
  `submit_draft` tool → `{text, items}`; `pushback()` → `str | None` (D-29).
- **`gpt_writer.py`** — GPT-4o-mini as the harsher, bolder writer (D-03). Same
  interface; OpenAI strict `json_schema` enforces the severity enum server-side.
- **`orchestrator_tool.py`** — Claude as the editor with two manuscripts. Scores
  both (0–10, explicit anchors so it can't over-score — R-08/F-14), selects or
  **synthesises**, sets `direction`, judges `converged`. `adjudicate()` returns
  `(OrchestratorDecision, selected_text)` — the decision is a summary (no draft
  text; drafts live on disk, D-07 #3), `selected_text` is what the loop
  checkpoints. Pure claude/gpt picks use the chosen draft verbatim (no rewrite
  drift); synthesis returns the orchestrator-merged text (F-18). `keyword_coverage`
  is computed in code by the scorer (D-25), not asked of the model. Proposed
  `rubric_additions` are raw — the loop JD-validates + caps them via `rubric.py`.

## rubric.py — dynamic rubric updates (D-04, unchanged across the dual-writer rewrite)

- Orchestrator-proposed additions are **validated against the JD** ("implied or
  hallucinated?"), capped at `max_additions_per_iteration`, tracked as
  `RubricAddition` with provenance. De-dup before the model call.

## scorer.py — keyword coverage (deterministic, no API)

- **Token-subset matching, not exact-phrase** (D-25/F-10). `keyword_coverage` is
  per-section; `union_coverage` is CV-level (fraction covered anywhere) — the
  aggregate the loop uses for `IterationScore.keyword_coverage` (F-15).
