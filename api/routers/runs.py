"""api/routers/runs.py — tailoring run lifecycle + SSE progress (SPEC §12.2/§12.5).

UI Step 1: read-only views over the SessionStore (list / get). Run initiation and
the SSE progress stream are wired in UI Step 3 (they need the background-thread
pipeline runner + the thread→async event bridge).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(request: Request) -> list[dict]:
    """All known runs (newest first), as public snapshots."""
    sessions = request.app.state.sessions.list()
    return [s.public() for s in sorted(sessions, key=lambda s: s.created_at, reverse=True)]


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    sess = request.app.state.sessions.get(run_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return sess.public()


@router.post("")
def start_run() -> dict:
    raise HTTPException(status_code=501, detail="run initiation lands in UI Step 3")


@router.get("/{run_id}/stream")
def stream_run(run_id: str) -> dict:
    raise HTTPException(status_code=501, detail="SSE progress stream lands in UI Step 3")
