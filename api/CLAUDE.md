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

- `corpus.py` (`/api/corpus`) — Mode 1: stats, CV inventory, and the full CV
  write path (add / edit-metadata / replace / delete), all **synchronous JSON**
  (D-36/F-42). Add + Replace are two steps — `POST /upload`|`/replace` stage the
  `.docx` to `tmp/corpus/<token>/` and return the parsed section inventory (no
  writes), then `POST /confirm` embeds + stores and moves the file into
  `data/cvs/` + writes the sidecar (CLI/UI on-disk parity). The R-01 inventory
  gate sits between the two steps; ingest is deliberately **not** SSE (one CV is a
  single embed call — F-42). `PATCH /cvs/{filename}/metadata` edits in place: it
  patches the ChromaDB section metadata (not just the sidecar) because the list +
  retrieval filters read metadata from there. Ingest/metadata helpers live in
  `corpus/ingest.py` (imported at module scope so tests monkeypatch them). The three
  **read** endpoints are public; the five **mutating** ones (`/upload`, `/replace`,
  `/confirm`, `PATCH …/metadata`, `DELETE …`) carry `dependencies=[Depends(require_unlocked)]`
  — gated on the same owner capability cookie as full mode (**D-39/§12.8**, `api/security.py`),
  **403 fail-closed** when no key is configured or the cookie is missing/invalid. Any future
  corpus-mutating endpoint must add the same dependency.
- `runs.py` (`/api/runs`) — Mode 2: start a run, list/get, SSE progress `/stream`. **Run
  visibility & retention (D-40/§12.9):** the archive is **capability-aware** — `GET /archive`
  and `/{id}/detail|report|files` return only `public_demo` runs (list redacted) unless the
  request is unlocked (`verify_token` on `cv_full_mode`); a private run **404s** when locked
  (don't leak ids). **Exception — live-session grant (F-48):** `_viewable` also passes when a
  live in-memory `Session` exists for the run id, so whoever just ran a job (a non-owner) can
  view their own report/detail/downloads until the session is GC'd by TTL (the friends-run-live
  case). Tradeoff: timestamped run ids are guessable, so the grant is id-holder-wide during that
  window — fine for the demo; upgrade to a per-run view token if it ever matters. Mutations
  `PATCH /{id}/meta` (company_name/keep/public_demo), `DELETE /{id}`,
  `POST /cleanup` are `require_unlocked` (403). Visibility/retention flags live in a **mutable
  sidecar** `outputs/<run_id>/run_meta.json` (`api/run_meta.py`) — never in the append-only
  `run_log.jsonl` (audit ≠ context). Retention helpers in `api/archive.py` (`cleanup_runs` ages
  by the run-id timestamp, not mtime); auto-cleanup runs on startup (`main.py` lifespan) **only
  when `RUN_RETENTION_DAYS` is set** — unset ⇒ off, so the test client never deletes real runs.
- `hitl.py` (`/api/runs/{id}/hitl`) — resume a paused run with the human's decision
  (a single action dict → `Session.submit_hitl`). Shares the `/api/runs` prefix with
  `runs.py`; FastAPI merges them. Action shapes per checkpoint: SPEC §12.3 / F-31.
- `full_mode.py` (`/api/capabilities`, `/api/full-mode/unlock` + `/lock`) — the Full
  Mode Unlock Gate (D-38/F-44). Full (Sonnet) runs are gated on a **signed HttpOnly
  capability cookie**, not a per-run key: `unlock` validates `FULL_MODE_KEY` and sets the
  cookie; `runs.py:start_run` enforces it (**403 fail-closed** when unset/missing/invalid).
  Token sign/verify is `api/security.py` (stdlib HMAC, secret = `FULL_MODE_KEY`). The CLI
  is unchanged (`--key`). Never read the raw key from a run request — gate on the cookie.
  The **same** capability also authorises corpus writes via the `require_unlocked` dependency
  (`api/security.py`, D-39/§12.8) — one owner unlock, both powers; no second secret/cookie.

## HITL handler (UI Step 4, F-31)

`SSEHITL` (api/runner.py) implements the pipeline's `fit`/`review`/`formatting`
handler interface — the *same* one as `AutoHITL`/`TerminalHITL`, so `tailor/run.py`
is unchanged. Each method publishes a JSON payload via `Session.wait_hitl` and
**blocks the pipeline thread** until `POST /hitl`. Free-text → Haiku → shown back
via a `hitl_interpreted` event **before** applying (preview-before-apply, D-18);
`review` is a **multi-turn loop** (apply item / interpret → preview → confirm /
accept) because the preview must precede the revision. Interpretation + revision
reuse `phase4_hitl.{interpret_freetext,revise_section}` / `phase1.interpret_fit_response`
verbatim — no provider code in the endpoint. `POST /api/runs` takes `auto: bool`
(default false = conversational; `auto:true` → `AutoHITL`, start-to-finish demo).

## Tests

- `tests/test_api.py` — Starlette `TestClient`, in-process, **no real API calls**
  (mock providers exactly as the pipeline tests do). Session primitives are unit
  tested directly (event buffer/seq, TTL cleanup, the HITL thread handoff).
