# SPEC_LANGFUSE_INSTRUMENTATION.md
## Langfuse Instrumentation — cv-tailor + Job Radar

**Status:** **Phase A (cv-tailor) ✅ built 2026-06-12** — see §9 build notes + Findings Log F-53.
Phase B (Job Radar) pending.
**Prerequisite:** `SPEC_LANGFUSE_DEPLOYMENT.md` — the self-hosted server. ⚠ On the first live test the
ingest API returned 200 but the server logged `Failed to upload JSON to S3`: the trace-blob storage
**bucket was never created**. Created server-side 2026-06-12; "complete and healthy ✅" was premature.
**Build order:** cv-tailor first ✅, Job Radar second
**SDK version:** langfuse v4 — confirmed `langfuse==4.7.1` in the built image (`requirements.txt`: `langfuse>=4.0.0`)

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

> **⚠ Built differently — the code below is illustrative and does NOT match
> the implementation (§9 / F-53).** Decorating `launch_run` would orphan every
> phase span: OTel context is thread-local and `launch_run` only *spawns* the
> run thread, then returns. The root trace is opened **inside the worker thread**
> (`api/runner.target`), around `run_pipeline`, via the `tailor/telemetry.run_trace`
> context manager — not a decorator. It also claims a **deterministic trace id**
> (`Langfuse.create_trace_id(seed=run_id)`) so `attach_scores` can match it later.

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

### 3.2 Trace structure

Unchanged from original spec — see section 3.2 in previous version.
Two targets: extraction batch and scoring run.

### 3.3 Batch API pattern

The Batch API is async — spans must be created after results arrive.
In v4, use `start_observation()` (manual, no context shift) for the
post-hoc pattern:

```python
from langfuse import get_client
from cli.telemetry import is_enabled

def record_extraction_batch(batch_id: str, batch_results: list, metadata: dict):
    if not is_enabled():
        return
    lf = get_client()

    # Root trace span for the whole batch
    with lf.start_as_current_observation(
        as_type="span",
        name="extraction_batch",
        input=metadata,
    ) as batch_span:
        with propagate_attributes(metadata={
            "batch_id": batch_id,
            "date": metadata["date"],
            "jd_count": str(len(batch_results)),
        }):
            for result in batch_results:
                with lf.start_as_current_observation(
                    as_type="span",
                    name="jd_extraction",
                    input={"job_id": result.job_id, "company": result.company},
                ) as jd_span:
                    with lf.start_as_current_observation(
                        as_type="generation",
                        name="claude_extraction",
                        model="claude-opus-4-8",
                        input=result.prompt,
                    ) as gen:
                        gen.update(
                            output=result.completion,
                            usage_details={
                                "input_tokens": result.usage.input,
                                "output_tokens": result.usage.output,
                            }
                        )
                    # Attach validation score
                    lf.api.scores.create(
                        trace_id=batch_id,
                        observation_id=jd_span.id,
                        name="validation_passed",
                        value=1 if result.validated else 0,
                        data_type="NUMERIC",
                    )
```

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

## 6. Definition of Done — cv-tailor (Phase A) — ✅ code complete

1. A completed cv-tailor run produces a trace in Langfuse with: **(code verified; awaiting a live
   run now the S3 bucket exists)**
   - ✅ `run_id` in trace metadata — enables cross-system lookup
   - ✅ Phase spans for Phase 0, 1, 3, 4 (plus 2, 5, 6 — the trace tree is complete)
   - ✅ LLM generations with model names + token counts (captured at the `helpers` chokepoint,
     so every Claude/GPT call across all phases is covered; Phase-3 generations nest per iteration)
   - ✅ `fit_score`, `coverage_score`, `cv_quality_score` as trace scores
   - ✅ `job_radar_fit_score` attached when the run came from Job Radar
2. ✅ Tracing disabled cleanly when `LANGFUSE_PUBLIC_KEY` is absent (every `telemetry.*` call no-ops)
3. ✅ All existing cv-tailor tests pass unchanged (328 untraced; +46 re-run *traced* to validate the
   enabled path — `tests/conftest.py` keeps the suite untraced by default)

**✅ Verified live (2026-06-12).** A full-mode run on a real job renders the complete trace in the
cv-tailor Langfuse project: the `cv_tailor_run` root (~8–10 min) with nested
`phase2_initial_draft → phase3_refinement → iteration_1/iteration_2` spans, and generations for
every model (`mistral_extraction`, `claude-sonnet-4`, `gpt-4o-mini`, `claude-haiku-4-5`) with
token/latency. The final blocker was a run-path flush bug, not config — see §10.6 / F-54.

## 7. Definition of Done — Job Radar (Phase B)

1. Completed extraction batch produces trace with child spans per JD
2. Scoring run produces trace with dimension breakdown per JD
3. All existing Job Radar tests pass unchanged (440)
4. Traces queryable alongside cv-tailor traces in Langfuse UI

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

## 9. Build notes — cv-tailor (Phase A, 2026-06-12, F-53)

The code blocks in §2 are **illustrative** (originally written against SDK v2; the phase
signatures never matched the real pipeline either). What was actually built:

**One module imports the SDK — `tailor/telemetry.py`.** The observability analogue of
`helpers.py`: phases, the runner, and the provider helpers trace *through* its context
managers (`run_trace` / `span` / `open_span` / `generation` / `set_metadata` /
`set_generation` / `attach_scores`). Every call is a **clean no-op when `LANGFUSE_PUBLIC_KEY`
is unset**, swallows its own errors (observability must never break a run), and **never masks
an exception from the wrapped body** (records it on the span, then re-raises).

**Root trace on the run thread, not on `launch_run` (the key fix).** OTel context is
thread-local; `launch_run` only spawns the daemon thread that runs the pipeline. So
`telemetry.run_trace(...)` wraps `run_pipeline` **inside `api/runner.target()`**. It claims a
**deterministic trace id** (`Langfuse.create_trace_id(seed=run_id)`) so `attach_scores` — which
runs after the thread's work and has no trace object to call — re-derives the same id via
`lf.create_score(trace_id=…)`. `run_id` also rides in trace metadata as the cross-system key.

**Generations at the `helpers` chokepoint.** `claude_complete`/`gpt_complete` wrap their
provider call in a generation at the exact point where token usage is already tapped for
`cost.py` (D-02). Result: *every* Claude/GPT call across all phases gets a generation with real
token counts, with **no signature changes** to the writer/orchestrator tools (which weren't
returning usage). Phase 0's Mistral call doesn't go through those helpers, so its generation is
created in `run.py` from the usage `analyse_jd` already returns.

**Phase-3 iteration spans via `open_span` (no loop re-indent).** The iteration body is ~175
lines; rather than re-indent it into a `with`, `telemetry.open_span()` returns `(obs, close)`
and is entered/closed linearly (`__enter__` activates the span in the OTel contextvar, so the
writer/orchestrator generations still nest under the right iteration). A mid-iteration exception
leaves that one span unclosed — acceptable, the run is already aborting.

**Tests stay untraced via `tests/conftest.py`.** `docker-compose.yml` loads `.env` (which carries
the real key) into the `cli` container, so the suite would otherwise run *traced* and export
mock-data spans to prod. `conftest.py` pops `LANGFUSE_PUBLIC_KEY` before any `is_enabled()` call;
a `CV_TAILOR_TRACE_TESTS=1` escape hatch keeps the key for deliberate enabled-path validation.

**Host config.** The SDK's host precedence is `LANGFUSE_BASE_URL` → `LANGFUSE_HOST` → cloud
default. The deployment's `.env` already sets `LANGFUSE_BASE_URL` to the self-hosted URL, so no
`LANGFUSE_HOST` is needed (the spec's `.env` snippet naming `LANGFUSE_HOST` also works, lower
precedence).

**Deployment gotcha (cost an hour).** First live test: ingest API returned 200, but the server
logged `500 Failed to upload JSON to S3` — the trace-blob bucket didn't exist. The client was
correct; the server's object storage was unprovisioned. Created server-side 2026-06-12. Lesson:
a Langfuse "healthy" check must include a round-trip that actually persists a span to S3, not just
an API liveness probe.

**Files touched:** `+tailor/telemetry.py`, `+tests/conftest.py`, `requirements.txt`,
`tailor/helpers.py`, `tailor/run.py`, `tailor/phases/phase3_refinement.py`, `api/main.py`,
`api/runner.py`. No business logic, prompts, schemas, or existing tests changed.

---

## 10. Debug + operations — verifying the trace path at zero cost (F-54)

Getting traces to actually land took longer than wiring the SDK, because the failure modes were
all **server/network/config**, not code — and each was silent. This section is the runbook.

### 10.1 `GET /api/debug/trace` — the zero-cost path check

`api/main.py` exposes an unauthenticated debug endpoint that creates a minimal `debug_trace`
(empty root span + one `debug_score = 1.0`) and flushes it — **no LLM call, no pipeline, $0**. It
exercises the entire export path (init → trace → score → flush → server) and returns the verdict
as JSON:

```bash
docker exec cv-tailor-backend python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/debug/trace').read())"
# {"trace_id":"…","enabled":true,"host":"http://langfuse-langfuse-web-1:3000","auth_check":true,"error":null}
```

Read it as a decision tree — **the JSON is the diagnostic, not the logs** (see 10.3):
- `enabled:false` → `LANGFUSE_PUBLIC_KEY` not in the backend container's env.
- `auth_check:false` + `error` → host unreachable or keys invalid from inside the container.
- `auth_check:true` + a `trace_id` that **appears in the UI** → the whole path works.
- `auth_check:true` but the trace **isn't in the UI** → it's in a *different project* (see 10.4).

`localhost:8000` here is correct: it runs *inside* `cv-tailor-backend`, hitting uvicorn directly —
the right way to isolate "is the backend serving?" from the Caddy/nginx/tunnel chain. "Container
Started" (Docker) ≠ "uvicorn listening" (app ready), so allow a few seconds / look for
`Uvicorn running on http://0.0.0.0:8000` before calling it.

### 10.2 Networking — point the backend at the *internal* host

The homeserver runs cv-tailor behind a Cloudflare Tunnel → Caddy. The backend container **cannot**
hairpin to its own public URL (`https://langfuse.michel-portfolio.co.uk`) from inside the Docker
network. It must reach Langfuse over the **shared Docker network by container name**:

```
LANGFUSE_BASE_URL=http://langfuse-langfuse-web-1:3000     # internal; NOT the public https URL
```

Requires the cv-tailor backend and the Langfuse stack to share a network (here, the external
`caddy` network). SDK host precedence is `LANGFUSE_BASE_URL` → `LANGFUSE_HOST` → cloud default.

### 10.3 Startup must not block; logs are nearly invisible — so diagnose via the response

- **Never probe an external service in the FastAPI lifespan.** `init_langfuse()` originally called
  `auth_check()` (a synchronous, no-timeout HTTP call) at startup. uvicorn only binds `:8000`
  *after* lifespan startup completes, so a slow Langfuse host hung boot → the backend refused
  connections (`ConnectionRefusedError [111]`). Fix: `init_langfuse()` only constructs the client
  (fast, no network); `auth_check()` lives in `/api/debug/trace` (request-time, can't wedge boot).
- **App-logger INFO is swallowed under uvicorn.** `tailor.telemetry` logs propagate to the root
  logger, which has only the last-resort handler (WARNING+), so `docker logs … | grep langfuse`
  comes back empty even when tracing runs fine. That's why the decisive signals (`auth_check`,
  `trace_id`, `error`) are returned in the endpoint's **JSON body**, not relied on in logs.

### 10.4 One key pair per project — or traces land in the wrong dashboard

Langfuse API keys are **per project**. Reusing a single `pk-lf-…`/`sk-lf-…` pair across cv-tailor
*and* Job Radar sends both apps' traces into whichever project those keys belong to — `auth_check`
still returns `true` (the keys are valid), the traces just appear under the *other* project. Each
app must use **its own project's keys** (§2.1 / §3.1 specify one project each). Symptom:
`auth_check:true`, endpoint returns a `trace_id`, but the cv-tailor dashboard is empty.

### 10.5 The full failure chain we hit (server/config + one run-path bug)

1. Trace-blob **S3 bucket didn't exist** → ingest 200 but `Failed to upload JSON to S3` (§9).
2. Backend used the **public URL** it couldn't reach from inside Docker → fixed to the internal host (10.2).
3. **`auth_check` in startup** hung the lifespan → backend refused `:8000` (10.3).
4. **Shared key pair** → traces landed in the Job Radar project's dashboard (10.4).
5. **Unflushed root span** → `/api/debug/trace` traced but real runs didn't (10.6).

Each looked like "no traces" with no obvious error. The `/api/debug/trace` JSON (`enabled` →
`auth_check` → `trace_id` → check the *right* project) collapses 1–4 into a quick triage; #5 was
the run-path-only bug it couldn't catch (the debug endpoint flushes after its span closes).

### 10.6 The run-path flush bug — debug traced, real runs didn't

After 1–4 were fixed, `/api/debug/trace` landed traces but **real runs still didn't**. Cause: in
`api/runner.target()`, `attach_scores()` (which calls `flush()`) runs *inside* the
`with run_trace` block — so it flushes while the **root span is still open**. The root span only
closes when the `with` exits, and nothing flushed after that, so the completed root span depended
on Langfuse's periodic exporter — but the daemon run-thread ends right there. The debug endpoint
never hit this because it flushes *after* its span closes. Fix: `run_trace` flushes in a `finally`,
after its own root span closes (`tailor/telemetry.run_trace`). **Lesson: a span isn't exported
until it's *ended*; if the producing thread is about to die, flush *after* the root closes, not
before.** Diagnosed with three WARNING-level logs (INFO is dropped by uvicorn, 10.3) bracketing the
run path — ENTER / root-span-created / attach_scores-flushed — which showed the first two firing
but never the third *for a completed run*.
