"""tailor/telemetry.py — Langfuse observability (opt-in, no-op when unconfigured).

The ONE module that imports the langfuse SDK — the observability analogue of
`helpers.py` for providers. Phases, the runner, and the provider helpers trace
*through* the context managers here, so when `LANGFUSE_PUBLIC_KEY` is absent every
call is a clean no-op and the langfuse import surface stays in one place
(SPEC_LANGFUSE_INSTRUMENTATION §2.3).

SDK: langfuse **v4** (OTel-based). Two facts drive the design:

1. **Nesting is OTel-context, hence thread-local.** A child observation opened
   inside a parent's `with` block attaches to it automatically — but only within
   the SAME thread. A web run executes on a worker thread (`api/runner.target`),
   so the root trace is opened *there*, around `run_pipeline`, NOT by decorating
   `launch_run` (which only spawns the thread and returns). The spec's illustrative
   `@observe()` on `launch_run` would orphan every phase span (F-53).

2. **Deterministic trace id from `run_id`.** `Langfuse.create_trace_id(seed=run_id)`
   gives a stable trace id, so the root span claims it (`trace_context`) and
   `attach_scores` re-derives the same id to hang scores on the right trace after
   the run — there is no persistent trace object to call once the run thread exits.

Observability must NEVER break the pipeline: every public helper guards on
`is_enabled()` and swallows its own errors, and the observation context managers
never mask an exception raised by the wrapped body (they record it, then re-raise).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys

log = logging.getLogger("tailor.telemetry")

__all__ = [
    "is_enabled", "init_langfuse", "run_trace", "span", "open_span", "generation",
    "set_metadata", "set_generation", "attach_scores", "flush",
]


def is_enabled() -> bool:
    """True iff Langfuse credentials are configured. The single tracing on/off gate."""
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY"))


def init_langfuse() -> None:
    """Initialise the Langfuse global singleton once at app startup. No-op (and
    import-safe) when unconfigured; a failed init disables tracing, never crashes boot."""
    if not is_enabled():
        return
    try:
        from langfuse import Langfuse
        Langfuse()
        # SDK host precedence is LANGFUSE_BASE_URL → LANGFUSE_HOST → cloud default.
        host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com"
        log.info("Langfuse tracing enabled (host=%s)", host)
    except Exception:                                  # observability must not break startup
        log.exception("Langfuse init failed; tracing disabled")


# --------------------------------------------------------------------------- #
# No-op stand-in + safe observation entry                                     #
# --------------------------------------------------------------------------- #

class _Null:
    """Stand-in for a Langfuse observation when tracing is disabled — any attribute
    access (`.update(...)`, `.id`, …) is a silent no-op."""
    __slots__ = ()

    def update(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *a, **k: None


_NULL = _Null()


def _safe_close(stack: contextlib.ExitStack, exc_info) -> None:
    try:
        stack.__exit__(*exc_info)
    except Exception:                                  # a langfuse teardown error must not surface
        log.debug("langfuse observation teardown failed", exc_info=True)


@contextlib.contextmanager
def _entered(*cm_factories):
    """Enter each langfuse context manager in order and yield the FIRST one's value
    (the observation). Disabled / setup failure → yield `_NULL`. NEVER masks an
    exception from the wrapped body: it records it on the spans, then re-raises the
    original. Langfuse enter/exit errors are swallowed so tracing can't break a run."""
    if not is_enabled():
        yield _NULL
        return
    stack = contextlib.ExitStack()
    obs = _NULL
    try:
        for i, factory in enumerate(cm_factories):
            value = stack.enter_context(factory())
            if i == 0:
                obs = value
    except Exception:
        log.exception("langfuse observation setup failed; continuing untraced")
        try:
            stack.close()
        except Exception:
            pass
        yield _NULL
        return
    try:
        yield obs
    except BaseException:                              # body raised — record on spans, re-raise original
        _safe_close(stack, sys.exc_info())
        raise
    else:
        _safe_close(stack, (None, None, None))


def _strmeta(fields: dict | None) -> dict | None:
    """Stringify metadata values (langfuse trace metadata is `dict[str, str]`); drop None."""
    if not fields:
        return None
    return {k: str(v) for k, v in fields.items() if v is not None} or None


# --------------------------------------------------------------------------- #
# Context managers — trace root, phase spans, LLM generations                 #
# --------------------------------------------------------------------------- #

def _trace_meta(run_id: str, mode: str, job_radar_source: dict | None) -> dict:
    jr = job_radar_source or {}
    raw = {
        "run_id": run_id,
        "mode": mode,
        "job_id": jr.get("job_id"),
        "company": jr.get("company"),
        "job_radar_fit_label": jr.get("fit_label"),
        "job_radar_fit_score": jr.get("fit_score"),
    }
    return {k: str(v) for k, v in raw.items() if v is not None}


@contextlib.contextmanager
def run_trace(run_id: str, *, mode: str, job_radar_source: dict | None = None):
    """Open the per-run root trace (`cv_tailor_run`) — wrap the WHOLE pipeline on its
    run thread. Sets trace-level metadata (`run_id` is the cross-system lookup key) and
    claims a deterministic trace id derived from `run_id` so `attach_scores` can match it."""
    def obs_factory():
        from langfuse import Langfuse, get_client
        tid = Langfuse.create_trace_id(seed=run_id)
        return get_client().start_as_current_observation(
            as_type="span", name="cv_tailor_run", trace_context={"trace_id": tid})

    def attr_factory():
        from langfuse import propagate_attributes
        return propagate_attributes(
            trace_name="cv_tailor_run", metadata=_trace_meta(run_id, mode, job_radar_source))

    with _entered(obs_factory, attr_factory) as obs:
        yield obs


@contextlib.contextmanager
def span(name: str, *, metadata: dict | None = None):
    """A phase / sub-step span. Child generations (opened deeper on the same thread)
    nest under it automatically. Set late-arriving metadata via `set_metadata(obs, ...)`."""
    def factory():
        from langfuse import get_client
        return get_client().start_as_current_observation(
            as_type="span", name=name, metadata=_strmeta(metadata))

    with _entered(factory) as obs:
        yield obs


def open_span(name: str, *, metadata: dict | None = None):
    """Manually-managed sibling of `span`: returns `(observation, close)`. Use ONLY where a
    `with` block can't wrap the work — e.g. a long loop body that must not be re-indented
    (phase3's iteration loop). `__enter__` activates the span in the OTel context, so child
    generations still nest; the caller MUST call `close()` on the happy path. An uncaught
    exception leaves the span open (acceptable — the run is already aborting)."""
    if not is_enabled():
        return _NULL, (lambda: None)
    try:
        from langfuse import get_client
        cm = get_client().start_as_current_observation(
            as_type="span", name=name, metadata=_strmeta(metadata))
        obs = cm.__enter__()
    except Exception:
        log.exception("langfuse open_span failed; continuing untraced")
        return _NULL, (lambda: None)

    def close():
        try:
            cm.__exit__(None, None, None)
        except Exception:
            log.debug("langfuse span close failed", exc_info=True)

    return obs, close


@contextlib.contextmanager
def generation(name: str, *, model: str, input=None):
    """An LLM call span. Record output + token usage via `set_generation(obs, ...)`."""
    def factory():
        from langfuse import get_client
        return get_client().start_as_current_observation(
            as_type="generation", name=name, model=model, input=input)

    with _entered(factory) as obs:
        yield obs


# --------------------------------------------------------------------------- #
# Updates + scores (no-op safe)                                               #
# --------------------------------------------------------------------------- #

def set_metadata(obs, **fields) -> None:
    """Attach (stringified) metadata to an observation. Safe on `_NULL` / errors."""
    if obs is _NULL:
        return
    try:
        meta = _strmeta(fields)
        if meta:
            obs.update(metadata=meta)
    except Exception:
        log.debug("langfuse set_metadata failed", exc_info=True)


def set_generation(obs, *, output=None, input_tokens=None, output_tokens=None) -> None:
    """Record a generation's output and token usage. Safe on `_NULL` / errors."""
    if obs is _NULL:
        return
    try:
        kwargs: dict = {}
        if output is not None:
            kwargs["output"] = output
        if input_tokens is not None or output_tokens is not None:
            kwargs["usage_details"] = {"input": int(input_tokens or 0),
                                       "output": int(output_tokens or 0)}
        if kwargs:
            obs.update(**kwargs)
    except Exception:
        log.debug("langfuse set_generation failed", exc_info=True)


def attach_scores(run_id: str, metrics: dict | None, job_radar_source: dict | None = None) -> None:
    """Attach the run's scores to its trace after completion (SPEC §2.6). Best-effort —
    never raises. `fit_score`/`coverage_score` are 0–1, `cv_quality_score` is 0–10;
    `job_radar_fit_score` is normalised 0–10 → 0–1 to compare against `fit_score`."""
    if not is_enabled():
        return
    try:
        from langfuse import Langfuse, get_client
        lf = get_client()
        tid = Langfuse.create_trace_id(seed=run_id)
        m = metrics or {}
        scores: list[tuple[str, float]] = []
        for name in ("fit_score", "coverage_score", "cv_quality_score"):
            v = m.get(name)
            if v is not None:
                scores.append((name, float(v)))
        jr_fit = (job_radar_source or {}).get("fit_score")
        if jr_fit is not None:
            scores.append(("job_radar_fit_score", float(jr_fit) / 10))
        for name, value in scores:
            lf.create_score(name=name, value=value, trace_id=tid, data_type="NUMERIC")
        lf.flush()
    except Exception:
        log.exception("langfuse attach_scores failed for run %s", run_id)


def flush() -> None:
    """Flush pending observations (best-effort)."""
    if not is_enabled():
        return
    try:
        from langfuse import get_client
        get_client().flush()
    except Exception:
        log.debug("langfuse flush failed", exc_info=True)
