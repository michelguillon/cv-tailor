# CLAUDE.md — tests/

Run inside the container: `docker compose run --rm cli pytest tests/`.

## Conventions

- **No real API calls in the default run.** Every provider call is mocked.
  LLM-gated tests (if any) are marked and excluded from the default run, mirroring
  the Week 2 split (deterministic pytest + a few LLM-gated).
- **Deterministic pytest from Step 2 onward.** Step 0–1 verify by running; from
  the scorer on, behaviour is unit-tested.
- `test_schemas.py` — one round-trip per schema in `models.py`, plus guards for
  the D-07/D-11 schema corrections. When you add a dataclass, add its round-trip
  here.
- `test_scorer.py` — `keyword_coverage()` at section level.
- `test_corpus.py` — section extraction, metric verification, metadata
  sanitisation, de-dup skip logic (mock ChromaDB / fixture `.docx`).
- `test_phase0.py`…`test_phase6.py` — per-phase unit tests with mocked LLM
  responses. Section freeze logic must be deterministic: same input → same freeze.
- `test_phases.py` — the fully-mocked **end-to-end** `run_pipeline` pass (Phase
  0→6, all three SDK providers faked in one run, `AutoHITL`). Patches the four
  seams — `phase0.get_mistral_client`, `helpers.get_{anthropic,openai}_client`,
  `run.all_sections` — with prompt-aware fakes that also count calls, so the cost
  footer is checked against known token usage. Cost accuracy, freeze determinism,
  and `replay` are verified here (Step 9, F-27).

Use `tmp_path` for any filesystem output; never write into the repo's
`outputs/`.
