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
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api.runner import launch_run
from api.session import TERMINAL, SessionError
from tailor.config import ConfigError, load_config, resolve_run_config
from tailor.run_context import new_run_id

router = APIRouter(prefix="/api/runs", tags=["runs"])


class StartRunRequest(BaseModel):
    jd_text: str
    mode: str = "demo"
    key: str | None = None
    max_iterations: int | None = None


@router.get("")
def list_runs(request: Request) -> list[dict]:
    sessions = request.app.state.sessions.list()
    return [s.public() for s in sorted(sessions, key=lambda s: s.created_at, reverse=True)]


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    session = request.app.state.sessions.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return session.public()


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
