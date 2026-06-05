# CLAUDE.md — api/ (FastAPI backend, SPEC §12)

The Web UI backend. Wraps the **same** `tailor` pipeline the CLI runs. Read the
root `CLAUDE.md` first. Built incrementally per SPEC §12.6 (UI Steps 1–6).

## Load-bearing conventions

- **Import the pipeline, never subprocess** (RFI entry 15/16). Endpoints call
  `tailor.run.run_pipeline` / `corpus.*` directly. The provider/tool abstractions
  (D-02) already hide everything else.
- **One `SessionStore` on `app.state.sessions`** for the process lifetime; routers
  read it off `request.app.state`. Never a module-global store (untestable, leaks
  across the app lifecycle).
- **A `Session` is VOLATILE coordination only.** Durable artifacts stay on disk in
  `outputs/<run_id>/` (the checkpoint substrate, D-07/R-06) exactly as in a CLI run.
  The session holds the SSE event buffer + the HITL handoff primitives, GC'd by TTL.
- **Async-HITL is two cross-thread channels** (`api/session.py`): the pipeline runs
  in a background thread; events flow thread→SSE via a `Condition` (+ replay buffer,
  so a late browser misses nothing); the human's decision flows endpoint→pipeline via
  an `Event`. The pipeline pauses at each checkpoint (`wait_hitl`) until `submit_hitl`.
- **Blocking pipeline calls go through `asyncio.to_thread`** in async endpoints
  (UI Step 3) — never block the event loop. SSE uses `sse-starlette`; nginx needs
  `proxy_buffering off` in prod (SPEC §7.5).
- **HITL is preview-before-apply** (D-18), same as the CLI: free text is interpreted
  (Haiku), shown back, and only then resumes the run.

## Routers (all under /api)

- `corpus.py` (`/api/corpus`) — Mode 1: stats, CV inventory, ingest (SSE), delete.
- `runs.py` (`/api/runs`) — Mode 2: start a run, list/get, SSE progress `/stream`.
- `hitl.py` (`/api/runs/{id}/hitl`) — resume a paused run with the human's decision.
  Shares the `/api/runs` prefix with `runs.py`; FastAPI merges them.

## Tests

- `tests/test_api.py` — Starlette `TestClient`, in-process, **no real API calls**
  (mock providers exactly as the pipeline tests do). Session primitives are unit
  tested directly (event buffer/seq, TTL cleanup, the HITL thread handoff).
