"""api/routers/hitl.py — conversational HITL resume (SPEC §12.3, UI Step 4).

POST /api/runs/{id}/hitl hands the human's decision to the paused pipeline thread
via `Session.submit_hitl`. The decision is a small action dict; all interpretation
(Haiku for free text) and revision happen on the pipeline thread inside `SSEHITL`
(api/runner.py), so this endpoint stays thin and never touches a provider.

Action shapes by checkpoint (the front end sends exactly one):
  fit_assessment : {"action": "proceed"|"override"|"stop"} | {"action":"freetext","text":...}
  section_review : {"action": "accept"} | {"action":"apply_item","index":N}
                 | {"action":"interpret","text":...}             (→ preview, no apply)
                 | {"action":"apply_freetext","section_id":..,"instruction":..}
  formatting     : {"action": "approve"|"reject"}

Shares the /api/runs prefix with the runs router; FastAPI merges them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.session import SessionError

router = APIRouter(prefix="/api/runs", tags=["hitl"])


class HITLRequest(BaseModel):
    action: str
    text: str | None = None
    index: int | None = None
    section_id: str | None = None
    instruction: str | None = None


@router.post("/{run_id}/hitl")
def submit_hitl(run_id: str, body: HITLRequest, request: Request) -> dict:
    session = request.app.state.sessions.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    try:
        session.submit_hitl(body.model_dump(exclude_none=True))
    except SessionError as exc:                 # not awaiting input (already resumed / terminal)
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "run_id": run_id, "status": session.status}
