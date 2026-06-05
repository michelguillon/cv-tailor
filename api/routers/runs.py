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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api import archive
from api.runner import launch_run
from api.session import TERMINAL, SessionError
from tailor.config import ConfigError, load_config, resolve_run_config
from tailor.run_context import new_run_id

router = APIRouter(prefix="/api/runs", tags=["runs"])

OUTPUT_DIR = "outputs"


class StartRunRequest(BaseModel):
    jd_text: str
    mode: str = "demo"
    key: str | None = None
    max_iterations: int | None = None


@router.get("")
def list_runs(request: Request) -> list[dict]:
    sessions = request.app.state.sessions.list()
    return [s.public() for s in sorted(sessions, key=lambda s: s.created_at, reverse=True)]


# NB: declared before "/{run_id}" so the literal path isn't captured as a run id.
@router.get("/archive")
def list_archive() -> list[dict]:
    """All completed runs on disk (replay/showcase) — works for preserved demo runs."""
    return archive.list_runs(OUTPUT_DIR)


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    session = request.app.state.sessions.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return session.public()


@router.get("/{run_id}/detail")
def run_detail(run_id: str) -> dict:
    """Replay payload from outputs/<run_id>/: summary + iteration scores + reasoning + cv_md."""
    detail = archive.run_detail(OUTPUT_DIR, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no output for run {run_id!r}")
    return detail


@router.get("/{run_id}/report")
def run_report(run_id: str):
    """The Phase-6 HTML report (4 tabs), served inline for the output panel iframe."""
    path = archive.run_file(OUTPUT_DIR, run_id, "cv_final.html")
    if path is None:
        raise HTTPException(status_code=404, detail=f"no report for run {run_id!r}")
    return FileResponse(path, media_type="text/html")


@router.get("/{run_id}/files/{name}")
def run_download(run_id: str, name: str):
    """Download cv_final.md or cv_final.html as an attachment."""
    path = archive.run_file(OUTPUT_DIR, run_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no file {name!r} for run {run_id!r}")
    return FileResponse(path, filename=name)


@router.post("", status_code=201)
def start_run(body: StartRunRequest, request: Request) -> dict:
    if not body.jd_text.strip():
        raise HTTPException(status_code=400, detail="jd_text is empty")
    # Validate mode/key synchronously so a bad key fails the POST, not the run thread.
    try:
        resolve_run_config(load_config(), mode=body.mode, key=body.key,
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

    launch_run(store, session, body.jd_text, mode=body.mode, key=body.key,
               max_iterations=body.max_iterations)
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
