# CLAUDE.md — cv-tailor (project root)

Multi-model CV tailoring orchestrator (Week 3 portfolio). A CLI tool: given a
job description and a corpus of CV versions, it produces a tailored CV with a
full audit trail and an HTML reasoning trace.

## Sources of truth — read before changing anything

- **`docs/SPEC_ORCHESTRATOR.md`** — *what* the system does. Architecture,
  schemas (§4), phases (§5), CLI (§6), structure (§7), Docker (§7.5).
- **`docs/LEARNING_NOTES_ORCHESTRATOR.md`** — *why*. Decision log (`D-xx`),
  reuse analysis (`R-xx`), and the Findings Log (`F-xx`, added during build).

These are maintained **throughout** the build, not retrospectively. When a build
finding changes or confirms a decision, add an `F-xx` entry to the Findings Log:
what was found, which `D-xx`/`R-xx` it affects, what changed. Do not silently
diverge from the spec — if a step reveals an ambiguity the spec doesn't resolve,
resolve it, record it, *then* write code.

## How this project runs — Docker, always (SPEC §6, §7.5)

Every CLI command and the test suite run inside the `cli` container. Python is
**3.13** (`python:3.13-slim`). Do not assume a host venv.

```bash
docker compose build
docker compose run --rm cli pytest tests/
docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
docker compose run --rm cli python -m tailor run --jd data/jd.txt --demo
```

One image, three entry points (CLI / FastAPI backend / pytest). The Web UI
(`api/` + `frontend/`) runs alongside: `docker compose up backend frontend` →
http://localhost:3000. Production is a two-file overlay (multi-stage nginx
frontend, no `--reload`): `docker compose -f docker-compose.yml -f
docker-compose.prod.yml up -d --build` (SPEC §7.5, F-32).

## Load-bearing conventions (don't break these)

- **LLMs are tools.** The orchestrator (Claude Sonnet; Haiku in demo) calls
  `critique_cv()`, `extract_keywords()` etc. as tools that return typed objects.
  The provider is an implementation detail *inside* the tool — the orchestrator
  never sees it (D-02). New cross-provider work goes behind a tool in `tailor/tools/`.
- **`call_with_retry()` wraps every API call, every provider.** It lives in
  `tailor/helpers.py`. The orchestrator never calls a provider client directly
  (R-05). This is load-bearing from the first real run, not polish.
- **Checkpoint pattern.** Each phase writes its output to `outputs/<run_id>/`
  before the next phase starts. Drafts are section files on disk, *not* fields
  on `PipelineOutput` (D-07 #3). Ingestion checkpoints per CV (ChromaDB persists
  after each CV; refines R-06's per-section guidance for a 7-CV corpus); the loop
  checkpoints per iteration (R-06).
- **Audit trail ≠ context.** Reasoning is logged to `run_log.jsonl` via
  `tailor/audit.py` and **never injected back into the messages array** (D-06).
- **Schemas are cross-provider contracts.** All dataclasses live in
  `tailor/models.py` with the `Serializable` mixin. A schema change has a large
  blast radius (D-07). Validate LLM-produced structured output against its schema
  *before* it touches anything downstream; retry once, then surface to the human
  (R-09).
- **Section is the unit of work** — drafting, critique, scoring, convergence, and
  freezing are all per-section (D-12). Static sections are copied verbatim and
  never enter the loop (D-13).
- **Never ship fabrication (D-18, F-34/F-35).** Tailoring SELECTS and re-emphasises the
  candidate's real content — it never invents or relabels it (no fabricated titles, sectors,
  metrics, or JD keywords the source doesn't support). This is enforced at three points: the
  writer prompts (`tools/writer_common.TRUTHFULNESS_RULES`, shared by Phase 2 + 3), the
  orchestrator (grounds each draft against the raw corpus source), and a verification gate
  (`tools/verifier.py`) that flags any unsupported claim into the review, the report's
  Grounding tab, and the CLI before anything ships. The raw source is persisted per section
  at Phase 2 (`sections/<id>_source.md`) as the ground truth.
- **CVCM is framing-only (§3.9/D-33/F-36).** The optional `candidate/value_creation_model.md`
  (gitignored, auto-loaded, `tailor/candidate.py`) is candidate-authored context threaded into
  Phases 1/2/3. It reorders/reframes the candidate's REAL content — it is NEVER a fact source, and
  a claim drawn from it but absent from the CV is still flagged by the verifier. The framing-only
  guardrail (`CVCM_FRAMING_NOTE`) must stay forceful: a gentle version leaked the model's wording
  straight into the CV (F-36).
- **HITL is preview-before-apply.** Show what will change, then ask. Never apply
  silently.
- **Config-driven, not code-branching.** Demo vs full and all thresholds come
  from `config.yaml` via `tailor/config.py` (D-08, §3.7).
- **No LangChain / no LangGraph.** Orchestration is built manually (§11).

## Build discipline

Follow the build sequence in SPEC §8 / the session prompt. Each step produces
something independently testable; **don't proceed until the current step's
verification passes.** Step 0–1 verify by running; pytest from Step 2 onward.

## Secrets & data — never commit

`.env`, `data/cvs/`, `data/chroma/`, `outputs/`, `tmp/` are gitignored. Don't add
real CVs, API keys, or run outputs to git.

## Directory-specific guidance

Sub-package `CLAUDE.md` files add local conventions: `tailor/`, `corpus/`,
`tests/`. `api/CLAUDE.md` and `frontend/CLAUDE.md` are added during the UI phase
(SPEC §7).
