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
- `test_phases.py` — per-phase unit tests with mocked LLM responses. Section
  freeze logic must be deterministic: same input → same freeze decision.
- Cost-tracking and `replay` are verified here too (Step 9).

Use `tmp_path` for any filesystem output; never write into the repo's
`outputs/`.
