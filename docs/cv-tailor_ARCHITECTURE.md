# cv-tailor — Architecture
## Consolidated Technical and Functional Reference

**Repository:** cv-tailor  
**Status:** Complete — pipeline (Steps 0–9) + web UI + security gates deployed  
**Deployment:** M720q home server · Docker Compose · FastAPI + React  
**Last updated:** post-UI (D-38/D-39/D-40)

---

## Contents

1. [What the system does](#1-what-the-system-does)
2. [System overview](#2-system-overview)
3. [Pipeline phases](#3-pipeline-phases)
4. [Dual-writer refinement loop](#4-dual-writer-refinement-loop)
5. [Schemas](#5-schemas-summary)
6. [User journeys](#6-user-journeys)
7. [Data flows](#7-data-flows)
8. [Web UI](#8-web-ui)
9. [Security model](#9-security-model)
10. [Deployment](#10-deployment)
11. [Key architectural decisions](#11-key-architectural-decisions)
12. [Reuse from prior projects](#12-reuse-from-prior-projects)

---

## 1. What the system does

cv-tailor takes a job description and a corpus of CV versions and produces a
tailored CV with a full audit trail. It is not a wrapper around a chat API.
It is a multi-agent decision-support system combining retrieval, evaluation,
grounding, orchestration, and human review.

**The problem it solves:** the manual workflow of fit-checking, keyword gap
analysis, iterative critique across multiple models, and formatting validation
— currently spread across Claude Code, ChatGPT, and manual Word editing —
produces better results than any single-model pass, but takes 45–90 minutes
per application and produces no audit trail. cv-tailor automates the
multi-model workflow, preserves the human review gates that matter, and
produces a traceable record of every decision.

**Three operating modes:**
```bash
# One-time corpus setup
docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/

# Main workflow
docker compose run --rm cli python -m tailor run --jd data/jd.txt --demo
docker compose run --rm cli python -m tailor run --jd data/jd.txt --key KEY

# Web UI
docker compose up backend frontend
# → http://server:3000
```

---

## 2. System overview

```
Entry points
─────────────────────────────────────────────────────────────
Browser (React + Vite + shadcn)     CLI (python -m tailor)
         │  :3000                              │
         │  /api/* SSE                         │ direct import
         ▼                                     │
FastAPI backend :8000 ─── import ─────────────▼
(SSE · HITL router                    tailor package
 asyncio.to_thread                    phases 0–6 · corpus · audit
 security gates D-38/39/40)           call_with_retry() all calls
         │                                     │
         └──────────────────┬──────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         Mistral AI    Anthropic      OpenAI
         mistral-small  Sonnet · Haiku  gpt-4o-mini
         embeddings     orchestrator    challenger
                        primary writer  writer
              │
              ▼
         ChromaDB (embedded, persistent)
         cv_corpus collection · cosine metric
         one document per CV section
              │
         Filesystem (bind-mounted)
         data/cvs/ · data/chroma/ · outputs/ · candidate/ · budgets.yaml
```

**Three providers, three justified roles:**

| Model | Role | Justification |
|---|---|---|
| mistral-small + mistral-embed | JD extraction · embeddings · scoring | Week 1 integration reuse; cheap structured tasks |
| claude-sonnet-4-6 | Orchestrator + primary writer | Complex multi-step reasoning; cross-turn consistency |
| gpt-4o-mini | Challenger writer | Empirically harsher drafts; different training prior (D-28) |
| claude-haiku-4-5 | Formatting · HITL interp · demo orchestrator | Fast, cheap, ~20× cheaper than Sonnet |

---

## 3. Pipeline phases

Seven deterministic phases wrap a single agentic loop. Phases 0–2 and 4–6
always run in order. Phase 3 is agentic: the orchestrator decides when to stop,
which writer's text to use, and whether to extend the scoring rubric.

```
Phase 0  JD Analysis         Mistral-small extracts keywords → ScoringRubric v1
         ~0.4s · <$0.01

Phase 1  Fit Assessment      Mistral embeddings → metadata-filtered ChromaDB retrieval
         ~2s · $0.01         → section-level composition recommendation (best source
                              per section_type across all CVs, not a single CV)
                              Outcome: strong · partial · no_fit (stops pipeline)
                              CVCM value-alignment notes if CVCM present
                              ⏸ HITL #1 — conversational, Haiku-interpreted

Phase 2  Initial Draft       Claude Sonnet drafts each non-static section from
         ~8s                 its recommended source CV. Word budget clamped to
                              source length (D-27/F-13). role_line split out
                              deterministically (D-32/F-29). Static sections copied.

Phase 3  Refinement Loop     Dual-writer loop (see §4)
         ~60–90s             ⏸ (no pause — runs to convergence or max_iterations)

Phase 4  Human Review        Section status display · unresolved items ·
                              lettered options + free-text [e] → Haiku interprets
                              ⏸ HITL #2 — conversational, Haiku-interpreted

Phase 5  Haiku Validation    Formatting · length check · grounding report
                              (Grounded Coverage % + Unsupported Claims count)
                              ⏸ HITL #3 — binary Approve/Reject only

Phase 6  Output Generation   Section assembly by position · role_line re-attached
                              cv_final.html (6 tabs) · cv_final.md · run_log.jsonl
```

**Termination table (F-16 validated):**
```
keyword_delta < 0.05 AND quality_delta < 0.5  →  convergence
iteration == max_iterations                    →  hard stop
zero major items (both writers, last iter)     →  soft-stop permitted
```

---

## 4. Dual-writer refinement loop

The core architectural novelty. Two independent writers draft every active
section simultaneously. One orchestrator adjudicates. Writers can push back.

```
Per active section, per iteration:

Step 1 — Dual write
  Claude (writer role)   →  WriterDraft { text, items[], pushback }
  GPT-4o-mini (writer)   →  WriterDraft { text, items[], pushback }
  Both receive: section text · JD + rubric · word budget · LoopMemory
                orchestrator direction (None on iter 1) · CVCM (optional)
                is_final_iteration flag

Step 2 — Orchestrator adjudication (Claude Sonnet, orchestrator role)
  Sees: both drafts + rubric
  Produces OrchestratorDecision:
    selected_base ("claude"|"gpt"|"synthesis")
    synthesis_notes (if synthesis)
    direction for next iteration
    claude_quality + gpt_quality (0–10, anchored scale)
    keyword_coverage of selected text
    converged flag (True = section frozen)
    rubric_additions (max 2, JD-validated — guard is load-bearing: F-28
                      showed Sonnet proposes 30+ per run; all rejected)
  Selected text written to disk: <section_id>_v<n>.md
  Per-writer drafts also written: <section_id>_<writer>_v<n>.md
  CVCM tiebreak: when |claude_quality − gpt_quality| < 1.0, CVCM
                 used as secondary selection factor (D-33)

Step 3 — Writer pushback (if not final iteration)
  Both writers receive orchestrator decision + direction
  May return pushback: str | None (one exchange only)
  Orchestrator reads pushbacks; may revise direction

Step 4 — Section freeze + convergence check
  Section frozen when: converged=True OR zero major items both writers
  Frozen sections excluded from subsequent iterations (cost efficiency)
  F-28: Sonnet never froze sections; loop stopped by aggregate plateau
        → dual-signal plateau is the safety net when freeze contributes nothing
```

**LoopMemory forwarded each iteration (structured state, not prose):**
```python
rejected_suggestions: list[str]     # prevents re-litigation
orchestrator_directions: list[str]  # trajectory context
frozen_sections: list[str]
iteration_scores: list[IterationScore]
```

**Synthesis behaviour (F-28 validated):**
- Iter 1: 8/8 synthesis (drafts diverge, orchestrator merges)
- Iter 2: 4/8 synthesis + 4/8 claude verbatim (drafts converge, orchestrator picks)
- Pattern: synthesis when drafts diverge; verbatim when they stabilise

**Cost (F-28, Airwallex JD, Sonnet mode, 2 iterations, 8 sections):** ~$0.79

---

## 5. Schemas summary

Key types (full definitions in `tailor/models.py`):

```python
# Writer output
WriterDraft { writer, section_id, text, version, pushback, items: list[CritiqueItem] }
CritiqueItem { section, severity ("major"|"minor"), issue, suggestion, source_writer }

# Orchestrator output
OrchestratorDecision { section_id, selected_base, direction, synthesis_notes,
                       claude_quality, gpt_quality, keyword_coverage,
                       converged, rubric_additions }

# Section state
CVSection { section_id, section_type, position, static, word_count, line_count }
SectionScore { section_id, keyword_coverage, claude_quality, gpt_quality,
               selected_writer, converged, current_version }
IterationScore { iteration, section_scores, keyword_coverage, critique_score,
                 quality_delta, sections_converged, sections_active }

# Fit assessment
FitAssessment { outcome ("strong"|"partial"|"no_fit"), recommended_sections,
                gaps: list[FitGap], overall_fit_score, value_alignment_notes }
FitGap { requirement, gap_type, addressable, severity, reason }

# Loop
LoopMemory { rejected_suggestions, orchestrator_directions,
             frozen_sections, iteration_scores }

# Run output
PipelineOutput { run_id, mode, jd_raw, jd_analysis, fit_assessment,
                 final_rubric, iterations, converged, convergence_reason,
                 cost_breakdown, value_alignment_notes }
```

**ChromaDB:** one document per CV section (not per CV). CV-level metadata
replicated on every section document. Collection: `cv_corpus`, cosine metric.
Distance metric is immutable at collection creation (R-03).

---

## 6. User journeys

### Tailoring run

```
User                                System
────────────────────────────────────────────────────────────────
Paste JD → [Start]
                                    Phase 0+1: JD analysis + fit
                                    ↓ SSE progress
⏸ Review fit (HITL #1)             ← chat panel: section mix, gaps, fit %
  Proceed / adjust / stop
  (free-text → Haiku interprets)
                                    Phase 2: initial draft
                                    Phase 3: dual-writer loop (live SSE)
                                    ↓ iteration progress, section status
⏸ Section review (HITL #2)        ← chat panel: status table, unresolved items
  [a–d] or [e] free-text            options + escape hatch
  (Haiku interprets [e])
                                    Phase 4: Haiku formatting + grounding
⏸ Formatting approval (HITL #3)   ← diff shown: Approve / Reject (binary)
                                    Phase 6: output generation
[Download cv_final.md]              → sticky card + 6-tab HTML output
```

**no_fit path:** Phase 1 stops the pipeline. Plain-English explanation shown.
Human can override and proceed. No further API spend without explicit confirmation.

### Corpus management

```
Add CV:    Upload .docx → YAML metadata form → Preview sections → ⏸ Confirm gate
           → ChromaDB write + .yaml sidecar → budgets.yaml re-derived

Edit metadata: [Edit Metadata] button → YAML form pre-filled → Save → .yaml only
               (no ChromaDB write; no section inventory)

Replace .docx: [Replace .docx] button → delete existing ChromaDB entries
               → same 4-step Add CV flow

Delete:    [Delete] → DELETE /api/corpus/{filename} → all ChromaDB entries removed
```

Section inventory confirmation gate is load-bearing (R-01/D-36): silent parse
failures (< 4 sections on a 2-page CV) are caught here before ChromaDB writes.

---

## 7. Data flows

### JD → final CV

```
Raw JD text
  ↓ Phase 0 (Mistral)
JDAnalysis + ScoringRubric v1
  ↓ Phase 1 (Mistral embed + Sonnet)
FitAssessment { recommended_sections, gaps, outcome, value_alignment_notes }
  ↓ HITL #1
Phase 2: section_id_v0.md per active section (role_line split out → manifest)
  ↓
Phase 3: WriterDraft (Claude) + WriterDraft (GPT) per section per iteration
  → OrchestratorDecision per section
  → section_id_v<n>.md (selected/synthesised)
  → section_id_<writer>_v<n>.md (per-writer for inspection)
  → LoopMemory updated
  ↓ HITL #2
Phase 5: formatting corrections + grounding report
  ↓ HITL #3
Phase 6: section assembly (ordered by CVSection.position, role_line re-attached)
  → cv_final.md  (clean, for sending)
  → cv_final.html (6 tabs + sticky summary card)
  → run_log.jsonl (audit trail, D-06: separate from context)
```

### Corpus ingestion

```
.docx file + .yaml sidecar
  ↓ python-docx parser (vocabulary + size split; NO heading styles, F-04)
CVSection list { section_id, section_type, position, word_count, static }
  ↓ Section inventory gate (human confirms; warns if < 4 sections)
  ↓ mistral-embed per section
ChromaDB write (cv_corpus, cosine, metadata sanitised before write R-04)
  ↓ post-commit
budgets.yaml re-derived (min/max/target word counts per section_type)
.yaml sidecar written to data/cvs/ (keeps CLI + UI on-disk state in sync)
```

---

## 8. Web UI

### Two modes (Run is default tab)

**Mode 1 — Run (default)**
JD textarea → [Start] → SSE progress timeline → HITL checkpoint panels inline
→ output panel (sticky summary card + 6 tabs)

**Mode 2 — Corpus management**
CV list with per-row [Edit Metadata] / [Replace .docx] / [Delete]
[+ Add CV] button → 4-step upload flow with section inventory gate

### SSE events
```json
{"type": "phase_start", "phase": "phase3_refinement", "iteration": 2}
{"type": "section_update", "section_id": "experience_acme", "status": "converged", "version": 3}
{"type": "phase_complete", "phase": "phase1_fit_assessment", "hitl_required": true}
{"type": "hitl_ready", "checkpoint": "fit_assessment", "payload": {...FitAssessment...}}
{"type": "run_complete", "run_id": "...", "cost_total_usd": 0.048}
```

**Reconnect resilience (F-55):** the stream traverses Cloudflare → tunnel → Caddy → nginx, any of
which can drop a long-lived connection. The client lets EventSource auto-reconnect (a transient
"Reconnecting…" badge on `readyState===CONNECTING`, a hard error only on `CLOSED`) and dedupes the
backend's buffer replay by event `seq`; the backend sends a `ping` every 10s, a `connected` event
on open (early bytes), and `id: <seq>` per event. A drop is a self-healing blip, not a fatal error.

### HITL interaction patterns

| Checkpoint | Interaction | Haiku call? |
|---|---|---|
| HITL #1 — Fit | Free text → interpreted into structured decision | Yes — shows interpretation before resuming |
| HITL #2 — Sections | Lettered options [a–d] + [e] free-text escape hatch | Only for [e] |
| HITL #3 — Formatting | Approve / Reject buttons | No — binary maps directly |

### Output panel

Sticky summary card (always visible, all tabs):
```
🟡 Fit: Partial (58%)  ·  ✓ Grounded Coverage: 36%  ·  ⚠ Unsupported Claims: 1
Status: Review Required  ·  Run: run_20260606_114928
```

Six tabs:
- **Fit** (default) — CVCM value alignment · transferable skills · gaps
- **CV** — clean assembled CV · copy-to-clipboard
- **Changes** — per-section diffs · claude_vN vs gpt_vN vs selected
- **Scores** — keyword_coverage + quality per section per iteration
- **Reasoning** — collapsible ReasoningEntry audit trail by phase
- **JD** — raw job description verbatim (traceability)

### Architecture
```
frontend:3000 (React + Vite + shadcn) — nginx-alpine in prod
  └── /api/* → backend:8000 (FastAPI + SSE)
        └── import → tailor package
              └── ChromaDB + Mistral + Anthropic + OpenAI
```

Key: import not subprocess (R-10 / RFI L-15); `asyncio.to_thread` for all
pipeline calls; filesystem-backed sessions with TTL in `tmp/{run_id}/`.

---

## 9. Security model

**Design principle:** not full auth — a spend guard. The goal is to keep a
public recruiter-facing deployment inspectable while stopping anyone but the
owner from running expensive operations or mutating state.

**Full mode unlock gate (D-38):**
- Demo mode: open to all
- Full mode: one-time passphrase entry → signed HttpOnly capability cookie
  (`cv_full_mode`, HMAC-SHA256, `exp` 30 days)
- `GET /api/capabilities` → `{demo_available, full_configured, full_unlocked}`
  drives UI to show / prompt-to-unlock / hide full mode
- Backend refuses full runs (403) without valid cookie even if UI bypassed
- Fail closed: no `FULL_MODE_KEY` env var → demo-only deployment

**Corpus write gate (D-39):**
- Read operations (browse corpus, view sections): public
- Write operations (add / replace / delete CV, edit metadata): require same
  capability cookie as full mode — one unlock, both powers
- CLI corpus ingestion bypasses the API gate (uses `corpus.ingest` directly)
- Fail closed: no `FULL_MODE_KEY` → corpus is view-only via web

**Run visibility and retention (D-40):**
- Public visitors see only runs flagged `public_demo` (redacted metadata)
- Owner (same capability cookie) sees all runs with full metadata + controls
- Per-run mutable flags in `run_meta.json` sidecar: `public_demo`, `keep`,
  `company_name` (orthogonal to `mode`)
- `company_name` precedence (F-47): manual (run form / edit) → Phase-0 name
  inferred from the JD (`JDAnalysis.company_name`, no extra LLM call) → "Unknown company"
- Private runs: filtered from list/detail (404, not 403 — run IDs not revealed)
- Cleanup: startup-triggered only when `RUN_RETENTION_DAYS` env var set;
  `keep` and `public_demo` runs are protected; age from run_id, not mtime

---

## 10. Deployment

**Directory structure (key paths):**
```
cv-tailor/
├── tailor/              ← core package: phases 0–6, tools, models, helpers
├── corpus/              ← ingestion + retrieval
├── api/                 ← FastAPI: routers (corpus, runs, hitl), security
├── frontend/            ← React + Vite + shadcn
├── candidate/           ← CVCM and durable artifacts (gitignored)
├── data/cvs/            ← source .docx files (gitignored)
├── data/chroma/         ← ChromaDB (gitignored)
├── outputs/             ← run outputs (gitignored)
├── tmp/                 ← session state with TTL (gitignored)
├── budgets.yaml         ← derived at ingestion
├── Dockerfile           ← Python 3.13-slim; one image for pipeline + API
├── docker-compose.yml   ← dev: bind-mounts, --reload
└── docker-compose.prod.yml  ← prod overlay: nginx, no-reload, restart policies
```

**Two compose files (same pattern as RFI):**
- `docker-compose.yml` — dev: bind-mounts, Vite dev server, `--reload`
- `docker-compose.prod.yml` — overlay: nginx-alpine frontend (~30MB image),
  `uvicorn` without `--reload`, `restart: unless-stopped`
- nginx: `proxy_buffering off` required for SSE (RFI learning)

**Homeserver deployment:**
```bash
git clone <repo> /srv/cv-tailor && cd /srv/cv-tailor
cp .env.example .env && $EDITOR .env   # ANTHROPIC_API_KEY, OPENAI_API_KEY,
                                        # MISTRAL_API_KEY, FULL_MODE_KEY
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**Bind-mounted state (back up these):**
`data/chroma/` · `data/cvs/` · `candidate/` · `budgets.yaml` · `.env`

**Update:**
```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**Observability — Langfuse tracing (opt-in, F-53):** when `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` are set, each run emits one Langfuse v4 trace
(`cv_tailor_run`): a span per phase, a span per Phase-3 iteration, a generation per LLM call
with token counts, and `fit_score`/`coverage_score`/`cv_quality_score`/`job_radar_fit_score`
as trace scores. `run_id`+`job_id` ride in metadata as cross-system join keys (to Job Radar
traces). Unset key ⇒ a clean no-op (the CLI and tests run untraced). `tailor/telemetry.py` is
the only SDK importer; the root trace is opened on the run's worker thread (`api/runner`, not
`launch_run` — OTel context is thread-local), with generations captured at the
`claude_complete`/`gpt_complete` chokepoint. **`GET /api/debug/trace`** (F-54) creates a minimal
trace with no LLM call ($0) and returns `{trace_id, enabled, host, auth_check, error}` — the
self-diagnosing path check for "no traces". On the homeserver the backend must reach Langfuse by
the **internal** Docker address (`LANGFUSE_BASE_URL=http://langfuse-langfuse-web-1:3000`), not the
public URL (no hairpin), and each app needs its **own** project's keys (shared keys → wrong
dashboard). Full design + the deploy/network runbook in `docs/SPEC_LANGFUSE_INSTRUMENTATION.md` §10.

---

## 11. Key architectural decisions

| ID | Decision | Load-bearing reason |
|---|---|---|
| D-01 | Hybrid pipeline + agentic loop | Determinism where task is known; agency where judgment required. Predictable cost, latency, HITL placement. |
| D-02 | LLMs as tools | Providers are implementation details of the tool. Orchestrator is provider-agnostic. Makes providers swappable. |
| D-04 | Dynamic scoring rubric (versioned, capped) | Evaluation criteria are a design decision. Cap (max 2 additions/iter) + JD-validation guard prevents rubric inflation. F-28 confirmed guard is load-bearing under Sonnet. |
| D-05 | Dual-signal convergence | Two orthogonal signals (keyword_delta + quality_delta). F-28: freeze path contributed nothing; aggregate plateau was the safety net. |
| D-06 | Audit trail separate from context | Reasoning logged to run_log.jsonl, never injected back. Context stays clean; audit stays complete. |
| D-12 | Section granularity | Unit of work is a section, not a CV. Different sections converge at different rates. Cost efficiency + recoverable intermediate versions. |
| D-13 | Static sections | Interests/education copied verbatim. Never critiqued, never versioned. Presence in assembly, invisible to loop. |
| D-17 | Section-level fit recommendation | Best source section per section_type across all CVs. Not a single base CV. Retrieval granularity matches recommendation granularity. |
| D-18 | Conversational HITL | Free-text interpreted by Haiku into structured decisions. Interpretation shown to human before resume. Pipeline gets structured input; human gets expressive interface. |
| D-21 | Promotion stacks | Multiple role lines before shared bullets captured as multi-line role_line. Each rendered bold. |
| D-26 | Haiku for dev, Sonnet for validation | Haiku ~⅓ cost, output quality close enough for iteration. Sonnet for final calibrated pass. |
| D-27 | Draft budget from source length | Word target = clamp(source_word_count, min, max). Anchored to actual source, not corpus median. |
| D-28 | Dual-writer loop | Two independent drafters (Claude + GPT), one orchestrator. Writers push back on direction (one exchange). LoopMemory forwarded. Mirrors the manual workflow that worked. |
| D-32 | Role/date lines split at Phase 2 | Structural facts must not enter draftable text. Split deterministically; re-attach verbatim at assembly. Same principle as static sections (D-13). |
| D-33 | CVCM optional overlay | Durable candidate artifact consumed not generated. Shifts from keyword optimisation toward value articulation. Optional: pipeline runs without it. |
| D-38 | Full mode capability cookie | Not full auth — a spend guard. Passphrase → signed HttpOnly cookie. One unlock per session. Fail closed. |
| D-39 | Corpus writes gated on same cookie | One capability, both powers. No second auth surface. |
| D-40 | Run visibility via filtering, not 403 | Public sees only public_demo runs. Owner sees all. Mutable sidecar for flags. Age from run_id not mtime. |

---

## 12. Reuse from prior projects

**Week 1 — RAG pipeline:**
- Mistral embeddings + ChromaDB persistence (same client, same pattern)
- `call_with_retry()` wrapper
- Key finding applied: document parsing discipline (F-04 — CV corpus has no
  heading styles; uses vocabulary+size split instead, R-01/R-02)

**Week 2 — Finance Agent:**
- Tool abstraction pattern extended to cross-provider orchestration
- JSONL audit trail format (D-06)
- Checkpoint discipline (per-section for ingestion, per-iteration for loop)
- HITL patterns (preview-before-apply, injectable handler)
- Mode switching at config layer, not branching

**RFI Answer Builder:**
- FastAPI + SSE architecture (same shape)
- Import not subprocess principle
- Filesystem-backed sessions with TTL
- Semantic retrieval beats hybrid on small corpus (R-07 — confirmed here)
- LLM-as-judge over-scores without anchors (R-08 — addressed via score anchors)
- ChromaDB metadata cannot be None/empty (R-04)
- nginx `proxy_buffering off` for SSE (R-10)
- Production compose overlay pattern
