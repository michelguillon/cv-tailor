"""api/routers/hitl.py — conversational HITL resume (SPEC §12.3).

UI Step 1: stub. UI Step 4 wires POST /api/runs/{id}/hitl to Session.submit_hitl —
the human's response (free text or a button) is interpreted (Haiku, for free text),
shown back, and handed to the paused pipeline thread to resume. Same prefix as the
runs router (both group under /api/runs); FastAPI merges them cleanly.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/runs", tags=["hitl"])


@router.post("/{run_id}/hitl")
def submit_hitl(run_id: str) -> dict:
    raise HTTPException(status_code=501, detail="conversational HITL lands in UI Step 4")
