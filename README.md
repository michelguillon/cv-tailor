# cv-tailor

Multi-model CV tailoring orchestrator. Given a job description and a corpus of
your CV versions, it produces a tailored CV with a full audit trail and an HTML
reasoning trace — one command, a few human-in-the-loop checkpoints.

It's the Week 3 piece of a learning track: **LLMs as tools.** A Claude Sonnet
orchestrator calls other models the same way a tool-using agent calls a database —
GPT-4o-mini for section critique, Mistral for keyword extraction + embeddings,
Claude Haiku for the formatting gate. The provider is an implementation detail
*inside* each tool; the orchestrator only sees typed results.

## Architecture in one paragraph

Deterministic phases (JD analysis → fit assessment → initial draft → … →
validation → output) frame a single **agentic refinement loop**. The loop works
at **section granularity**: each section is drafted, critiqued, revised, scored,
and frozen independently. Termination is **dual-signal** — keyword coverage *and*
critique score must both plateau — with a soft-stop allowed only when the last
critique returns zero major issues. A **dynamic scoring rubric** can grow during
the loop (capped, validated, versioned) so the CV is measured against what the JD
*means*, not just what it says. All reasoning is logged to `run_log.jsonl`,
separate from model context.

## Stack

Python 3.13 · ChromaDB (RAG over CV sections) · Anthropic + OpenAI + Mistral ·
Jinja2 HTML output · Docker. No LangChain / LangGraph — orchestration is built
by hand.

## Quick start

```bash
cp .env.example .env        # ANTHROPIC_API_KEY, OPENAI_API_KEY, MISTRAL_API_KEY, FULL_MODE_KEY
docker compose build
docker compose run --rm cli pytest tests/

# ingest your CVs (.docx + a sidecar .yaml each — see ADAPTING.md), then tailor:
docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
docker compose run --rm cli python -m tailor run --jd data/jd.txt --demo
```

Outputs land in `outputs/<run_id>/`: `cv_final.html` (CV + changes + scores +
reasoning tabs), `cv_final.md` (clean CV), and `run_log.jsonl` (audit trail).

## Docs

- `docs/SPEC_ORCHESTRATOR.md` — architecture, schemas, phases, Docker setup.
- `docs/LEARNING_NOTES_ORCHESTRATOR.md` — decision log and build findings.
- `ADAPTING.md` — using it with your own CV corpus.

## Status

Under active build.
- **Step 0** — schemas + audit logger. Done.
- **Step 1** — corpus ingestion + retrieval. Done: 7 CVs → 83 sections in
  ChromaDB, length budgets derived, metadata-filtered semantic search. 86 tests.
- **Step 2** — JD analysis (Mistral, forced JSON) + section-level keyword
  scorer. Done: model chosen on evidence (4-JD eval), token-subset coverage
  matching. 103 tests.
- **Step 3** — fit assessment. Done: deterministic section-level mix (best CV
  variant per section, experience mixed per company) + Claude (Haiku/dev,
  Sonnet/full) for typed gaps + outcome via forced tool-use; soft seniority
  (D-23); HITL preview. 113 tests.

Next: Step 4 — initial draft (Claude, per-section, budget-aware).
