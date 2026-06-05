"""api/main.py — the FastAPI app (SPEC §12.5). UI Step 1.

Mounts the three routers under /api and exposes a health check. The pipeline is
imported, never shelled out (RFI entry 15/16). A single process-wide SessionStore
is shared via app.state so every router and the SSE stream see the same sessions.

Run (compose): uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import corpus, hitl, runs
from api.session import SessionStore

app = FastAPI(title="cv-tailor", version="0.1.0",
              summary="Multi-model CV tailoring orchestrator — Web UI backend (SPEC §12)")

# Dev CORS: the Vite dev server (localhost:3000) calls the backend (localhost:8000).
# Prod serves the bundle behind nginx same-origin, so this is dev-only breadth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"], allow_headers=["*"],
)

# One SessionStore for the process lifetime; routers read it off app.state.
app.state.sessions = SessionStore()


@app.get("/api/health")
def health() -> dict:
    """Liveness + a count of live sessions (cheap smoke test for the demo)."""
    return {"status": "ok", "service": "cv-tailor", "sessions": len(app.state.sessions.list())}


app.include_router(corpus.router)
app.include_router(runs.router)
app.include_router(hitl.router)
