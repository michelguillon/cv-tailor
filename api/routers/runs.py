"""api/routers/runs.py — tailoring run lifecycle + SSE progress (SPEC §12.2/§12.5).

POST /api/runs        → validate mode/key, create a session, launch the pipeline in a
                        background thread (AutoHITL for now), return the run_id.
GET  /api/runs        → list runs (newest first).
GET  /api/runs/{id}   → one run's public snapshot.
GET  /api/runs/{id}/stream → SSE: replays the session's event buffer then streams new
                        events live until the run reaches a terminal state.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api import archive
from api.run_meta import write_meta
from api.runner import launch_run
from api.security import FULL_COOKIE, full_mode_configured, require_unlocked, verify_token
from api.session import TERMINAL, SessionError
from tailor.config import ConfigError, load_config, resolve_run_config
from tailor.run_context import new_run_id

router = APIRouter(prefix="/api/runs", tags=["runs"])

OUTPUT_DIR = "outputs"
DEFAULT_RETENTION_DAYS = 7.0          # manual cleanup default when RUN_RETENTION_DAYS is unset


def _unlocked(request: Request) -> bool:
    """Whether this request holds a valid owner capability cookie (§12.9). verify_token
    already fails closed when no FULL_MODE_KEY is configured, so this is owner-or-nothing."""
    return verify_token(request.cookies.get(FULL_COOKIE))


def _viewable(request: Request, run_id: str) -> bool:
    """A run is viewable if the request is unlocked (owner), the run is a public demo, or a
    live session for it still exists. The session grant lets whoever ran a job see their own
    output (report + downloads) without an owner unlock — the friends-run-live case (§12.9):
    a non-owner can drive a run over SSE but, without this, every attempt to view the result
    404s. Access lasts only while the in-memory session is retained (GC'd by TTL after the run
    reaches a terminal state, api/session.py), then narrows back to owner-or-public.

    Tradeoff (accepted): run ids are timestamped and therefore guessable, so during the
    session window anyone holding/guessing the id can view a private run. Acceptable for the
    demo deployment; tighten to a per-run view token if that ever matters."""
    return (_unlocked(request)
            or archive.is_public(OUTPUT_DIR, run_id)
            or request.app.state.sessions.get(run_id) is not None)


class StartRunRequest(BaseModel):
    jd_text: str
    mode: str = "demo"
    max_iterations: int | None = None
    auto: bool = False          # True = AutoHITL (no pauses); False = conversational HITL (UI Step 4)
    company_name: str | None = None   # optional label for the run list (§12.9); editable later
    # No `key` field: full mode is gated on the capability cookie (D-38), not a per-run key.


class RunMetaPatch(BaseModel):
    """Owner edits to a run's visibility/retention sidecar (§12.9). None = leave unchanged."""
    company_name: str | None = None
    keep: bool | None = None
    public_demo: bool | None = None


@router.get("")
def list_runs(request: Request) -> list[dict]:
    sessions = request.app.state.sessions.list()
    return [s.public() for s in sorted(sessions, key=lambda s: s.created_at, reverse=True)]


# NB: declared before "/{run_id}" so the literal path isn't captured as a run id.
@router.get("/archive")
def list_archive(request: Request) -> list[dict]:
    """Completed runs on disk (replay/showcase), capability-aware (§12.9/D-40): public
    visitors see only `public_demo` runs (redacted); the owner sees all with full metadata."""
    unlocked = _unlocked(request)
    return archive.list_runs(OUTPUT_DIR, include_private=unlocked, redact=not unlocked)


@router.post("/cleanup", dependencies=[Depends(require_unlocked)])
def cleanup_runs() -> dict:
    """Delete stale private runs now (older than RUN_RETENTION_DAYS, default 7; keeps
    `keep`/`public_demo`). Owner-only — the on-demand half of retention (§12.9)."""
    days = archive.retention_days_env() or DEFAULT_RETENTION_DAYS
    removed = archive.cleanup_runs(OUTPUT_DIR, days)
    return {"removed": removed, "count": len(removed), "max_age_days": days}


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    session = request.app.state.sessions.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return session.public()


@router.get("/{run_id}/detail")
def run_detail(run_id: str, request: Request) -> dict:
    """Replay payload from outputs/<run_id>/: summary + iteration scores + reasoning + cv_md.

    A private run 404s for a locked request (don't reveal its existence, §12.9)."""
    if not _viewable(request, run_id):
        raise HTTPException(status_code=404, detail=f"no output for run {run_id!r}")
    detail = archive.run_detail(OUTPUT_DIR, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no output for run {run_id!r}")
    return detail


@router.get("/{run_id}/report")
def run_report(run_id: str, request: Request):
    """The Phase-6 HTML report (4 tabs), served inline for the output panel iframe."""
    if not _viewable(request, run_id):
        raise HTTPException(status_code=404, detail=f"no report for run {run_id!r}")
    path = archive.run_file(OUTPUT_DIR, run_id, "cv_final.html")
    if path is None:
        raise HTTPException(status_code=404, detail=f"no report for run {run_id!r}")
    return FileResponse(path, media_type="text/html")


@router.get("/{run_id}/files/{name}")
def run_download(run_id: str, name: str, request: Request):
    """Download cv_final.md or cv_final.html as an attachment."""
    if not _viewable(request, run_id):
        raise HTTPException(status_code=404, detail=f"no file {name!r} for run {run_id!r}")
    path = archive.run_file(OUTPUT_DIR, run_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no file {name!r} for run {run_id!r}")
    return FileResponse(path, filename=name)


@router.patch("/{run_id}/meta", dependencies=[Depends(require_unlocked)])
def patch_run_meta(run_id: str, body: RunMetaPatch) -> dict:
    """Set a run's company_name / keep / public_demo (owner-only, §12.9). None = unchanged."""
    run_dir = archive.run_dir_if_exists(OUTPUT_DIR, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    meta = write_meta(run_dir, company_name=body.company_name,
                      keep=body.keep, public_demo=body.public_demo)
    return {"run_id": run_id, **meta}


@router.delete("/{run_id}", dependencies=[Depends(require_unlocked)])
def delete_run(run_id: str, request: Request) -> dict:
    """Delete a run's output dir (and drop any live session). Owner-only (§12.9)."""
    deleted = archive.delete_run(OUTPUT_DIR, run_id)
    request.app.state.sessions.delete(run_id)        # best-effort volatile cleanup
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return {"deleted": run_id}


@router.post("", status_code=201)
def start_run(body: StartRunRequest, request: Request) -> dict:
    if not body.jd_text.strip():
        raise HTTPException(status_code=400, detail="jd_text is empty")
    # Full mode is gated on the capability cookie (D-38, §12.7), never a per-run key.
    # Fail closed: no server key configured, or no valid cookie → forbidden (403).
    key = None
    if body.mode == "full":
        if not full_mode_configured():
            raise HTTPException(status_code=403, detail="full mode is not available on this server")
        if not verify_token(request.cookies.get(FULL_COOKIE)):
            raise HTTPException(status_code=403, detail="full mode is locked — unlock it first")
        key = os.environ["FULL_MODE_KEY"]      # cookie proven → resolve with the env key
    # Validate mode synchronously so a bad config fails the POST, not the run thread.
    try:
        resolve_run_config(load_config(), mode=body.mode, key=key,
                           max_iterations=body.max_iterations)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store = request.app.state.sessions
    run_id = new_run_id()
    for suffix in range(1, 100):                       # avoid same-second id collisions
        try:
            session = store.create(run_id, mode=body.mode)
            break
        except SessionError:
            run_id = f"{new_run_id()}_{suffix}"
    else:
        raise HTTPException(status_code=500, detail="could not allocate a run id")

    # Persist the visibility sidecar up front when a company label was supplied (§12.9). New
    # runs default private + not-kept; the run dir is the pipeline's, created here idempotently.
    if body.company_name:
        run_dir = Path(OUTPUT_DIR) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        write_meta(run_dir, company_name=body.company_name)

    launch_run(store, session, body.jd_text, mode=body.mode, key=key,
               max_iterations=body.max_iterations, auto=body.auto)
    return {"run_id": run_id, "mode": body.mode, "status": session.status}


@router.get("/{run_id}/stream")
async def stream_run(run_id: str, request: Request):
    session = request.app.state.sessions.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")

    async def generator():
        seq = 0
        while True:
            if await request.is_disconnected():
                break
            # events_since blocks (up to timeout) for new events; run it off the loop.
            events = await asyncio.to_thread(session.events_since, seq, timeout=5.0)
            for event in events:
                seq = event["seq"] + 1
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
            if session.status in TERMINAL and seq >= len(session.events):
                break

    return EventSourceResponse(generator())
