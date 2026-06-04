# CLAUDE.md — tailor/tools/

LLMs-as-tools and the deterministic scorer. The orchestrator calls these exactly
as the Week 2 agent called SQL queries (D-02). Read `tailor/CLAUDE.md` first.

## The tool contract

- **Each tool returns a typed `models.py` object; the provider is hidden.** The
  orchestrator never learns whether OpenAI, Anthropic, or Mistral ran inside
  (D-02). Provider details stay behind `helpers.py` — tools call `gpt_complete` /
  `claude_complete` / `embed_*`, never an SDK directly, so `call_with_retry` wraps
  every call (R-05).
- **Validate structured output before returning it; retry once, then raise** (R-09).
- **Prefer "leave state unchanged" over "corrupt state" on failure.** `rubric.py`
  returns the rubric untouched (same version) if the orchestrator call fails — a
  version bump in the audit trail therefore always means a real change.

## critique.py — section critique (GPT-4o-mini, D-03)

- GPT-4o-mini is deliberate: empirically harsher, less flattering CV feedback than
  Claude, and an independent second opinion (D-03).
- **Severity is defined in the prompt, not just the schema** (D-11) — the soft-stop
  depends on "zero `major` items", so `major`/`minor` must mean the same thing every
  iteration. Strict `json_schema` enums severity so the model can't emit anything
  else.
- **Score anchors are mandatory** (R-08): without explicit "9 = …, 7 = …, 5 = …"
  the LLM-judge over-scores everything 8+, flattening the convergence signal (F-14).
- **Length-budget items are appended deterministically in code** (D-14): code
  counts words, GPT judges content. `section_scores` is a list in the wire schema
  (strict mode needs `additionalProperties:false`), converted to a dict after parse.

## rubric.py — dynamic rubric updates (D-04)

- Critique-proposed additions are **validated against the JD by the orchestrator**
  ("is this implied, or hallucinated?"), capped at `max_additions_per_iteration`,
  tracked as `RubricAddition` with provenance. De-dup against existing keywords
  *before* the model call (no wasted call when nothing is new).

## scorer.py — keyword coverage (deterministic, no API)

- **Token-subset matching, not exact-phrase** (D-25/F-10): a multi-word keyword
  matches if its significant tokens (minus a stoplist) appear as whole tokens
  anywhere; single-token keywords need an exact whole-token match. Found by running
  on real CV × real JD, not fixtures — verify scorers against real data.
- `keyword_coverage` is per-section (fraction of the rubric in one section);
  `union_coverage` is CV-level (fraction covered anywhere) — the aggregate the
  refinement loop uses (F-15).
