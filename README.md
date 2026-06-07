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

## Web UI

A browser surface over the same pipeline (`docker compose up backend frontend` →
http://localhost:3000): **Tailor a CV** (paste a JD, watch the phases stream, handle
the human-in-the-loop checkpoints conversationally, read the tabbed report behind a
sticky fit/grounding summary card), **Runs** (browse/replay past runs), and **Corpus**
(add / edit-metadata / replace / delete CV versions, behind the section-inventory gate).

## Docs

- `docs/SPEC_ORCHESTRATOR.md` — architecture, schemas, phases, Docker + deploy (§7.5).
- `docs/LEARNING_NOTES_ORCHESTRATOR.md` — decision log (`D-xx`) and build findings (`F-xx`).
- `ADAPTING.md` — using it with your own CV corpus.

## Deployment

Runs on a homeserver behind Caddy + a Cloudflare Tunnel via the prod overlay
(`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backend
frontend`). The frontend nginx is the entry point and proxies `/api` to the backend; the
CV corpus is seeded out-of-band (scp the `.docx`/`.yaml`, then re-embed on the server).
SPEC §7.5 has the topology; the full runbook lives in `DEPLOY-cv-tailor.md`.

## Status

Feature-complete; preparing for deployment. The full pipeline (Phase 0→6) runs from the
CLI and the Web UI, over a 7-CV / 83-section ChromaDB corpus. Dual-writer refinement
(Claude + GPT-4o-mini, orchestrated), a source-grounded anti-fabrication trust layer
(writer rules → orchestrator gate → honest metric → verifier), and the demo/full mode
split are all in place. A single owner unlock (signed HttpOnly capability cookie) gates
full (Sonnet) runs, corpus write operations, and run management (delete / keep / publish),
so a public deployment stays browsable — showing only curated public-demo runs — but
read-only until unlocked (SPEC §12.7/§12.8/§12.9). Stale private runs are auto-cleaned by
an optional retention window. See the Findings Log in the learning notes for the build trail.
