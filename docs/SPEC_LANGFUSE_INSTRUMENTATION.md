# SPEC_LANGFUSE_INSTRUMENTATION.md
## Langfuse Instrumentation — cv-tailor + Job Radar

**Status:** Phase A (cv-tailor) ✅ verified live 2026-06-12 · Phase B (Job Radar) ✅ verified live 2026-06-13
**Prerequisite:** `SPEC_LANGFUSE_DEPLOYMENT.md` complete and healthy ✅
**Build order:** cv-tailor first ✅, Job Radar second ✅
**SDK version:** langfuse v4 — confirmed `langfuse==4.7.1` in both built images (`requirements.txt`: `langfuse>=4.0.0`)

> **Note:** This spec was originally written against SDK v2. All code blocks
> have been rewritten for SDK v4. The v4 SDK is OTel-based — the API is
> context manager and decorator driven, not object-chaining. The trace
> structure (section 2.2) is unchanged — only the Python code to produce it.

---

## 1. Instrumentation philosophy

**Trace what matters, not everything.**

The goal is evidence for the §7 research questions from the integration
spec — where do Job Radar and cv-tailor scores diverge, and why? That
means tracing at the decision boundary level: phase inputs/outputs,
LLM calls, scores attached to traces. Not every internal function.

Add granularity only where gaps appear after a few weeks of data.
Over-instrumenting at the start adds maintenance cost before you know
what's useful.

**SDK v4 pattern used: `@observe()` decorator for cv-tailor, manual
observations for Job Radar.** cv-tailor has a clean phase-based
architecture that maps naturally to decorators. Job Radar uses the
Batch API (async, not real-time) which requires manual observation
creation after results arrive.

---

## 2. cv-tailor instrumentation

### 2.1 Project

One Langfuse project: `cv-tailor`. API keys from deployment setup.

### 2.2 Trace structure

Each cv-tailor run = one Langfuse trace. This is unchanged from the
original spec — only the code to produce it changes.

```
Trace: cv_tailor_run
  name = "cv_tailor_run"
  metadata = {
    run_id: "...",              ← stored in propagate_attributes, enables cross-system lookup
    mode: "demo" | "full",
    job_id: "sha256:..." | null,
    company: "Elastic" | null,
    job_radar_fit_label: "strong_fit" | null,
    job_radar_fit_score: 10 | null
  }

  Span: phase0_jd_analysis
    Generation: mistral_extraction
      model: "mistral-small" | "mistral-large"
      input: <JD text>
      output: <JDAnalysis JSON>
      usage: {input_tokens, output_tokens}

  Span: phase1_fit_assessment
    Generation: claude_fit_assessment
      model: "claude-sonnet-4-6" | "claude-haiku-4-5"
      input: <fit assessment prompt>
      output: <FitAssessment JSON>
      usage: {input_tokens, output_tokens}
    metadata = {
      outcome: "strong_fit" | "good_fit" | "partial" | "poor_fit",
      overall_fit_score: 0.72,
      cvcm_enabled: true
    }

  Span: phase2_cv_selection
    metadata = {
      candidates_evaluated: 3,
      selected_cv: "cv_director_2026.docx"
    }

  Span: phase3_refinement
    metadata = {iterations_run: 2, converged: true}

    Span: iteration_1
      Generation: claude_orchestrator
        model: "claude-sonnet-4-6"
        input: <orchestration prompt>
        output: <refinement decisions>
      Generation: haiku_section_rewrite  (0..N per iteration)
        model: "claude-haiku-4-5"
        input: <section + instructions>
        output: <rewritten section>
      metadata = {
        keyword_coverage: 0.35,
        critique_score: 7.2,
        sections_converged: 3,
        sections_active: 2
      }

    Span: iteration_2
      ... same structure ...

  Span: phase4_grounding
    Generation: claude_grounding_check
      model: "claude-haiku-4-5"
      input: <grounding prompt>
      output: <grounding result>
    metadata = {
      fabrication_flags: 0,
      grounded_coverage: 0.81
    }

  Span: phase5_cover_letter    (if generated)
    Generation: claude_cover_letter
      ...

  Span: phase6_final_assembly
    metadata = {
      output_format: "docx",
      sections_in_final: 8
    }

  Score: fit_score          = 0.56   (Phase 3 callback value)
  Score: coverage_score     = 0.35   (Phase 3 callback value)
  Score: cv_quality_score   = 8.1    (Phase 3 callback value)
  Score: job_radar_fit_score = 10    (from job_radar_source, if present)
```

### 2.3 SDK integration

Install:
```bash
pip install langfuse
```

Add to `requirements.txt`:
```
langfuse>=4.0.0
```

Create `tailor/telemetry.py` — initialises the client once and exposes
a helper to check if tracing is enabled. In SDK v4, `get_client()`
returns the global singleton; if no `LANGFUSE_PUBLIC_KEY` is set the
client is still returned but tracing is silently disabled.

```python
# tailor/telemetry.py
import os
from langfuse import get_client, Langfuse

def init_langfuse() -> None:
    """Initialise Langfuse client if credentials are present.
    Call once at app startup (e.g. in main or runner init).
    No-op if LANGFUSE_PUBLIC_KEY is absent — tracing disabled cleanly.
    """
    if os.getenv("LANGFUSE_PUBLIC_KEY"):
        Langfuse()  # initialises the global singleton

def is_enabled() -> bool:
    """Returns True if Langfuse credentials are configured."""
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
```

**Tracing is opt-in by config.** No `LANGFUSE_PUBLIC_KEY` in `.env` →
all `@observe()` decorators and context managers are no-ops. Tests run
without tracing. Production has it enabled. No mocking required.

### 2.4 Trace creation

In SDK v4, there is no explicit `lf.trace()` call. Instead, the root
`@observe()` decorated function *is* the trace. Trace-level metadata
is set via `propagate_attributes()`.

In `api/runner.py`:

```python
from langfuse import observe, propagate_attributes, get_client
from tailor.telemetry import is_enabled

@observe(name="cv_tailor_run")
def launch_run(run_id: str, job_radar_source: dict | None = None, mode: str = "demo", ...):
    # Set trace-level metadata — propagates to all child spans
    if is_enabled():
        with propagate_attributes(
            trace_name="cv_tailor_run",
            metadata={
                "run_id": run_id,
                "mode": mode,
                "job_id": job_radar_source.get("job_id") if job_radar_source else None,
                "company": job_radar_source.get("company") if job_radar_source else None,
                "job_radar_fit_label": job_radar_source.get("fit_label") if job_radar_source else None,
                "job_radar_fit_score": str(job_radar_source.get("fit_score")) if job_radar_source else None,
            }
        ):
            return run_pipeline(run_id, job_radar_source, mode, ...)
    else:
        return run_pipeline(run_id, job_radar_source, mode, ...)
```

> **v4 note on metadata values:** `propagate_attributes` requires
> `metadata` to be `dict[str, str]` — all values must be strings.
> Cast non-strings explicitly (e.g. `str(fit_score)`).

### 2.5 Phase spans

Each phase function gets an `@observe()` decorator. Because cv-tailor
phases are already separate functions, this is a one-line change per
phase. Child spans nest automatically via OTel context propagation.

```python
from langfuse import observe, get_client

@observe(name="phase0_jd_analysis", as_type="span")
def run_phase0(jd_text: str, ...):
    # instrument the Mistral call as a generation inside this span
    lf = get_client()
    with lf.start_as_current_observation(
        as_type="generation",
        name="mistral_extraction",
        model="mistral-small",
        input={"jd_text": jd_text},
    ) as gen:
        result = call_mistral(jd_text)
        gen.update(
            output=result,
            usage_details={"input_tokens": result.usage.input, "output_tokens": result.usage.output}
        )
    return result

@observe(name="phase1_fit_assessment", as_type="span")
def run_phase1(jd_analysis, cv_text, ...):
    lf = get_client()
    with lf.start_as_current_observation(
        as_type="generation",
        name="claude_fit_assessment",
        model="claude-sonnet-4-6",
        input={"jd_analysis": jd_analysis, "cv": cv_text},
    ) as gen:
        result = call_claude_fit(jd_analysis, cv_text)
        gen.update(
            output=result.assessment,
            usage_details={"input_tokens": result.usage.input, "output_tokens": result.usage.output}
        )
        # Update the parent span (phase1) with outcome metadata
        lf.update_current_observation(metadata={
            "outcome": result.assessment.fit_label,
            "overall_fit_score": str(result.assessment.overall_score),
        })
    return result

@observe(name="phase3_refinement", as_type="span")
def run_phase3(cv_draft, jd_analysis, ...):
    lf = get_client()
    for i, iteration in enumerate(run_iterations(...)):
        with lf.start_as_current_observation(
            as_type="span",
            name=f"iteration_{i+1}",
        ) as iter_span:
            # orchestrator call
            with lf.start_as_current_observation(
                as_type="generation",
                name="claude_orchestrator",
                model="claude-sonnet-4-6",
                input=iteration.orchestrator_prompt,
            ) as gen:
                decisions = call_orchestrator(iteration)
                gen.update(output=decisions)

            # section rewrites (0..N)
            for section in iteration.sections_to_rewrite:
                with lf.start_as_current_observation(
                    as_type="generation",
                    name="haiku_section_rewrite",
                    model="claude-haiku-4-5",
                    input={"section": section.name, "instructions": section.instructions},
                ) as gen:
                    rewritten = call_haiku_rewrite(section)
                    gen.update(output=rewritten)

            iter_span.update(metadata={
                "keyword_coverage": str(iteration.keyword_coverage),
                "critique_score": str(iteration.critique_score),
            })
```

### 2.6 Score attachment

In SDK v4, scores are attached via the API client, not the trace object.
Do this after the run completes in `api/runner.py`:

```python
from langfuse import get_client
from tailor.telemetry import is_enabled

def attach_scores(run_id: str, metrics: dict, job_radar_source: dict | None = None):
    if not is_enabled():
        return
    lf = get_client()
    scores = []
    if metrics.get("fit_score") is not None:
        scores.append({"name": "fit_score", "value": metrics["fit_score"]})
    if metrics.get("coverage_score") is not None:
        scores.append({"name": "coverage_score", "value": metrics["coverage_score"]})
    if metrics.get("cv_quality_score") is not None:
        scores.append({"name": "cv_quality_score", "value": metrics["cv_quality_score"]})
    if job_radar_source and job_radar_source.get("fit_score") is not None:
        scores.append({
            "name": "job_radar_fit_score",
            "value": job_radar_source["fit_score"] / 10  # normalise to 0–1
        })
    for score in scores:
        lf.api.scores.create(
            trace_id=run_id,
            name=score["name"],
            value=score["value"],
            data_type="NUMERIC",
        )
```

> **Why `lf.api.scores.create()`?** In v4, scores are attached via
> the REST API client rather than on a trace object, since there's no
> persistent trace object to call `.score()` on after the decorated
> function returns. The `run_id` ties the score back to the correct trace.

### 2.7 HITL decisions

Log HITL inputs as events on the current observation:

```python
from langfuse import get_client
from tailor.telemetry import is_enabled

def log_hitl_input(phase: str, hitl_text: str, interpretation: str):
    if not is_enabled():
        return
    lf = get_client()
    lf.create_event(
        name="hitl_input",
        metadata={
            "phase": phase,
            "input": hitl_text,
            "interpretation": interpretation,
        }
    )
```

### 2.8 What NOT to trace in cv-tailor

- Internal string manipulation, template rendering
- File I/O (reading/writing docx, json)
- The SSE event stream itself
- Individual token counts within a rewrite (aggregate at generation level)

---

## 3. Job Radar instrumentation

Build after cv-tailor instrumentation is confirmed working.

### 3.1 Project

One Langfuse project: `job-radar`. Separate from cv-tailor.

### 3.2 Trace structure — AS BUILT

> **As-built note (2026-06-13).** This section was rewritten to match the shipped
> code (`cli/telemetry.py`) after several deviations from the original sketch were
> discovered live. The trace *shapes* are as designed; the code below reflects the
> real SDK surface and the worker-ingestion requirement the sketch missed.

**THREE trace targets, not two** — the original spec had extraction + scoring, both
CLI-driven. The synchronous **manual ingest** (`POST /api/manual-ingest`) is a *separate
code path* in the FastAPI process (it runs `extract_one` + `score` inline, never the
CLIs), so it needs its own recorder or it is silently untraced:

| Target | Where | Trace name | Shape |
|---|---|---|---|
| `extraction_batch` | `cli/label.py` after `merge_results` | `extraction_batch` | root → `jd_extraction` span per JD → `claude_extraction` generation (Opus, tokens) → `validation_passed` score |
| `scoring_run` | `cli/score.py` | `scoring_run` | root → `jd_scoring` span per JD → `dimension_score` span per dimension |
| `manual_ingest` | `api/routers/manual_ingest.py` after persist | `manual_ingest` | root → `claude_extraction` generation (Haiku, tokens) → `jd_scoring` span → `dimension_score` spans → `validation_passed` score |

### 3.3 Three corrections the sketch missed (the load-bearing facts)

1. **`langfuse.trace.name` is REQUIRED for the worker to ingest a trace.** The original
   sketch set no trace name. A trace whose spans lack the `langfuse.trace.name` attribute
   uploads to MinIO but the worker never promotes it into ClickHouse — it silently never
   appears in the UI (diagnosed by diffing MinIO payloads against cv-tailor's working
   spans). Every root span MUST be wrapped in `propagate_attributes(trace_name=…)`,
   mirroring `tailor/telemetry.run_trace`. `start_as_current_observation` has **no**
   `trace_name` parameter (confirmed by introspection) — `propagate_attributes` is the
   mechanism.

2. **Use the proven SDK surface, not `lf.trace()` / `lf.api.scores.create()`.** The shipped
   code mirrors cv-tailor's verified-live calls: `Langfuse.create_trace_id(seed=…)` for a
   deterministic id, the root span claiming it via `trace_context={"trace_id": tid}`, and
   `lf.create_score(name=, value=, trace_id=, observation_id=, data_type="NUMERIC")` for
   scores. The langfuse SDK is imported lazily *inside* functions so `import cli.telemetry`
   works uninstalled, and `is_enabled()` gates every recorder to a clean no-op.

3. **Flush AFTER the root span closes.** The Batch API is async and the CLI exits
   immediately — there is no periodic exporter to fall back on. Each recorder builds its
   whole tree inside the root `with`, lets it close, then `lf.flush()`. (Manual ingest runs
   in the long-lived API process, but flushes the same way so the trace shows promptly.)

The canonical shape (see `cli/telemetry.record_extraction_batch` for the full version):

```python
from langfuse import Langfuse, get_client, propagate_attributes
lf = get_client()
tid = Langfuse.create_trace_id(seed=batch_id)
with lf.start_as_current_observation(
    as_type="span", name="extraction_batch",
    trace_context={"trace_id": tid}, input=metadata,
), propagate_attributes(trace_name="extraction_batch", metadata={...}):   # ← trace_name = worker requirement
    for row in rows:
        with lf.start_as_current_observation(as_type="span", name="jd_extraction", ...) as jd_span:
            with lf.start_as_current_observation(as_type="generation", name="claude_extraction",
                                                 model=row["model"], input=row["prompt"]) as gen:
                gen.update(output=row["completion"],
                           usage_details={"input": ..., "output": ...})
            lf.create_score(name="validation_passed", value=1.0 if row["validated"] else 0.0,
                            trace_id=tid, observation_id=jd_span.id, data_type="NUMERIC")
lf.flush()   # AFTER the root closes — CLI is about to exit
```

Rows are assembled by **pure** builders (`cli.label.build_trace_rows`,
`cli.score.build_scoring_rows`) that re-derive the scorer breakdown with `stage1_fit`
(read-only) — no business logic, prompt, or schema touched, and unit-testable without the
SDK. A `debug-trace` CLI/probe (`python -m cli.telemetry debug-trace`) exercises the full
init→trace→score→flush path at zero cost (it carries `auth_check`; `init_langfuse` never
does, as that sync probe would hang).

### 3.4 Cross-system linkage

Unchanged — cv-tailor trace is looked up by `run_id` which matches
the Langfuse trace name metadata. Both traces are independently
queryable and joinable via IDs stored in `corpus/cv_tailor_links.jsonl`.

---

## 4. Tests

### cv-tailor
- `@observe()` decorators are no-ops when `LANGFUSE_PUBLIC_KEY` is absent
- No test env changes needed — tracing silently disabled
- Run existing test suite; must pass unchanged
- Add one smoke test: set `LANGFUSE_PUBLIC_KEY` to a test project key,
  run one pipeline, confirm trace appears in Langfuse UI

### Job Radar
- Same pattern: no `LANGFUSE_PUBLIC_KEY` → no tracing
- Existing 440 tests must pass unchanged

---

## 5. Build order

### Phase A — cv-tailor (build first)
1. `pip install langfuse`, add `langfuse>=4.0.0` to `requirements.txt`
2. Create `tailor/telemetry.py`
3. Call `init_langfuse()` at app startup
4. Add `@observe(name="cv_tailor_run")` to `launch_run()` in `runner.py`
5. Add `propagate_attributes()` block with trace metadata
6. Instrument Phase 0 — `@observe` + generation context manager
7. Instrument Phase 1 — `@observe` + generation + metadata update
8. Instrument Phase 3 — `@observe` + iteration spans + generation per rewrite
9. Instrument Phase 4 — `@observe` + generation + metadata
10. Attach scores via `lf.api.scores.create()` after run completes
11. Deploy, run one real cv-tailor job, verify trace in Langfuse UI
12. Instrument remaining phases (2, 5, 6) — lower priority

### Phase B — Job Radar (after cv-tailor confirmed)
1. `pip install langfuse`, add to `requirements.txt`
2. Create `cli/telemetry.py` (same pattern as cv-tailor)
3. Instrument extraction batch (post-batch context managers)
4. Instrument scoring run (spans, no generations)
5. Deploy, run one extraction, verify traces

---

## 6. Definition of Done — cv-tailor (Phase A)

1. A completed cv-tailor run produces a trace in Langfuse with:
   - `run_id` in trace metadata — enables cross-system lookup
   - Phase spans for Phase 0, 1, 3, 4
   - LLM generations with token counts
   - `fit_score`, `coverage_score`, `cv_quality_score` as trace scores
   - `job_radar_fit_score` attached when run came from Job Radar
2. Tracing disabled cleanly when `LANGFUSE_PUBLIC_KEY` is absent
3. All existing cv-tailor tests pass unchanged

## 7. Definition of Done — Job Radar (Phase B) ✅ shipped 2026-06-13

1. ✅ Completed extraction batch produces trace with child spans per JD (`extraction_batch`)
2. ✅ Scoring run produces trace with dimension breakdown per JD (`scoring_run`)
3. ✅ **Manual ingest produces a `manual_ingest` trace** (extraction generation + scoring
   breakdown) — the separate API path, instrumented after it was found untraced
4. ✅ Every trace sets `langfuse.trace.name` via `propagate_attributes` — without it the
   worker drops the trace (does not reach ClickHouse / the UI)
5. ✅ All existing Job Radar tests pass with no `LANGFUSE_PUBLIC_KEY` (468; was 440 at spec time)
6. ✅ Traces queryable alongside cv-tailor traces in the Langfuse UI

**Forward work (post-close): refine *what* we trace.** The plumbing is done and verified;
the next iteration tunes granularity from real usage — e.g. capturing cost/latency on the
batch generations, trimming low-value metadata, and deciding whether dimension-level spans
earn their keep. Add granularity only where gaps appear (see §1) — don't pre-instrument.

---

## 8. What this enables

Once both systems are instrumented and 20+ linked runs exist:

**In the Langfuse UI:**
- Filter cv-tailor traces by `metadata.job_id` — see every run for a
  specific Job Radar role
- Compare `job_radar_fit_score` vs `fit_score` on the same trace
- See which Phase 3 iterations took longest and cost most
- See HITL decisions and how Haiku interpreted them

**Via direct DB query (PostgreSQL + ClickHouse):**
- Join traces by `job_id` to answer the §7 research questions
- Compute average divergence between Job Radar and cv-tailor fit scores
  by company, domain, or role type
- Identify systematic extraction failures (low `validation_passed` rate
  for specific companies)

This is the raw material for the Phase 4 redesign — multi-agent scoring
orchestration grounded in evidence rather than assumption.

---

## 12. Critical operational finding — `langfuse.trace.name` required by worker

Discovered during Job Radar Phase B. The Langfuse v3 worker uses
`langfuse.trace.name` as a routing signal to process OTel spans from MinIO
into ClickHouse. Spans without this attribute sit in MinIO indefinitely —
no error is logged anywhere.

**How it gets set:**
- `start_as_current_observation(name="your_name")` → sets it automatically ✅
- `lf.trace(name="...")` low-level API → does NOT set it ✗

Always use `start_as_current_observation` with an explicit name on the root
observation. Never use `lf.trace()` as the root span.

**Diagnostic — read the raw MinIO payload:**

```bash
docker exec langfuse-langfuse-minio-1 sh -c \
  "mc alias set local http://localhost:9000 \$MINIO_ROOT_USER \$MINIO_ROOT_PASSWORD \
   && mc cat local/langfuse/otel/<project_id>/<path>.json" 2>/dev/null
```

Look for `{"key":"langfuse.trace.name",...}` in the span attributes.
If absent — spans will not be processed into ClickHouse.

**Updated triage order (supersedes §11.3 step 2):**

1. Debug probe → `enabled` → `auth_check` → `trace_id`
2. Confirm `trace_id` in UI in the **right project**
3. Read raw MinIO JSON → confirm `langfuse.trace.name` present
4. Only now run one real job (token-spending step)
5. If debug traces land but real runs don't → flush/scope bug (§10.6)

---

## 13. Audit every pipeline entry point

During Job Radar Phase B, the manual job addition path was not instrumented
in the initial build — only the automated batch path was. Always check every
route that produces traceable work, not just the primary path. In Job Radar:
both `cli/label.py` (batch) and the manual addition path are instrumented.
