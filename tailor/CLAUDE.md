# CLAUDE.md — tailor/ (orchestration package)

The orchestrator and all phases/tools. Entry point: `python -m tailor`
(`__main__.py` → `run.py`). Read the root `CLAUDE.md` first.

## Layout

- `models.py` — **all** schemas (SPEC §4). Add new dataclasses here, in
  dependency order (a type must be defined before another type references it),
  inheriting `Serializable`. Every new schema needs a round-trip test in
  `tests/test_schemas.py`. `to_dict`/`from_dict` are generic and type-hint
  driven — no per-class serialisation code.
- `config.py` — `RunConfig` loaded from `config.yaml`. Mode differences are
  config values, not `if mode == "demo"` branches.
- `audit.py` — `AuditLogger` → `run_log.jsonl`. Pure, no API calls. Log a
  `ReasoningEntry` for every orchestrator decision; never feed it back into context.
- `helpers.py` — the three provider clients + `call_with_retry()`. **This is the
  only module that touches a provider SDK directly.**
- `phases/` — `phase0`…`phase6`. Deterministic, fixed order. The *only* agentic
  region is `phase3_refinement.py` (accept/reject critique, extend rubric, decide
  convergence).
- `tools/` — LLM-as-tool wrappers (`critique.py` → GPT-4o-mini; `scorer.py`;
  `rubric.py`). Each returns a typed object from `models.py`; the caller never
  learns which provider ran.

## Rules specific to this package

- A phase reads its inputs from the previous phase's checkpoint and writes its
  own checkpoint to `outputs/<run_id>/` before returning.
- Convergence is **dual-signal** (keyword_delta < 0.05 AND critique_delta < 0.5);
  soft-stop is permitted **only** when the last critique had zero `major` items
  (D-05, D-01). Don't add a third termination path without recording it.
- Rubric additions: max 2 per iteration, each validated against the JD before
  acceptance, tracked as `RubricAddition` with provenance (D-04).
- Demo mode swaps the Sonnet orchestrator for Haiku via config — same interface.
