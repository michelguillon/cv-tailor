# CLAUDE.md — tailor/ (orchestration package)

The orchestrator and all phases/tools. Entry point: `python -m tailor`
(`__main__.py` → `run.py`). Read the root `CLAUDE.md` first.

## Layout

- `models.py` — **all** schemas (SPEC §4). Add new dataclasses here, in
  dependency order (a type must be defined before another type references it),
  inheriting `Serializable`. Every new schema needs a round-trip test in
  `tests/test_schemas.py`. `to_dict`/`from_dict` are generic and type-hint
  driven — no per-class serialisation code.
- `run.py` — `run_pipeline()` sequences Phase 0→6 + the cost footer; HITL
  checkpoints are delegated to a handler (`TerminalHITL` for the CLI, `AutoHITL`
  for tests / `--yes`) so phases only *render*, never read stdin.
- `__main__.py` — the `python -m tailor` CLI (`run`, `replay`), click-based.
- `candidate.py` — `load_cvcm()`: loads the optional Candidate Value Creation Model
  (`candidate/value_creation_model.md`, gitignored). Framing-only context for Phases
  1/2/3 (§3.9/D-33/F-36); never a fact source. Holds `CVCM_FRAMING_NOTE` (keep it forceful).
- `config.py` — `load_config` + `resolve_run_config` → a `RunConfig`. Mode
  differences are config values, not `if mode == "demo"` branches (D-08); full mode
  is key-gated on `FULL_MODE_KEY` (§3.7).
- `audit.py` — `AuditLogger` → `run_log.jsonl`. Pure, no API calls. Log a
  `ReasoningEntry` for every orchestrator decision; never feed it back into context.
- `cost.py` — per-model cost **estimate** (D-08, F-08). `cost.track()` activates a
  tracker; `helpers` notes usage into it on every call. Side-channel, like audit —
  phases never touch it.
- `helpers.py` — the three provider clients + `call_with_retry()`. **This is the
  only module that touches a provider SDK directly** (and the one place usage is
  captured for `cost.py`). Generations are also captured here (the same usage
  chokepoint), so all Claude/GPT calls trace with token counts (F-53).
- `telemetry.py` — Langfuse observability (opt-in, SPEC_LANGFUSE_INSTRUMENTATION). **The only
  module that imports the langfuse SDK** (the observability analogue of `helpers.py`). Every
  trace/span/generation/score routes through its context managers, which are a **clean no-op when
  `LANGFUSE_PUBLIC_KEY` is unset** and never raise into the pipeline. The root trace opens on the
  run's worker thread (`api/runner`), generations at the `helpers` chokepoint. `debug_trace()`
  backs `GET /api/debug/trace` — a $0 path check (F-53/F-54). Flush *after* a span's scope closes,
  never block startup on `auth_check`, and keep diagnostics at WARNING (uvicorn drops INFO).
- `run_context.py` — per-run output dir, versioned section files, audit logger.
  The checkpoint substrate every phase writes through (`write_section`,
  `write_checkpoint`, `read_section`).
- `phases/` — `phase0`…`phase6`. Deterministic, fixed order; the only agentic
  region is `phase3_refinement.py`. Local conventions in `phases/CLAUDE.md`.
- `tools/` — LLM-as-tool wrappers: the dual-writer trio (`claude_writer.py`,
  `gpt_writer.py`, `orchestrator_tool.py`) plus `scorer.py` and `rubric.py`. Each
  returns a typed `models.py` object; the caller never learns which provider ran.
  Local conventions in `tools/CLAUDE.md`.

## Rules specific to this package

- **Checkpoint in, checkpoint out.** A phase reads its inputs from the previous
  phase's checkpoint and writes its own to `outputs/<run_id>/` before returning
  (R-06). Section drafts are versioned files on disk, never fields on
  `PipelineOutput` (D-07 #3).
- **Demo vs full is config, not branching.** Phases take the orchestrator model
  (Haiku/dev, Sonnet/full) and thresholds from `RunConfig`; never hardcode a model
  or `if mode == "demo"` (D-08, D-26).
- **Audit ≠ context.** Log every orchestrator decision as a `ReasoningEntry` via
  `audit.py`; never feed it back into the messages array (D-06).
