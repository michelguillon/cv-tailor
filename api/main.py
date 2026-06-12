"""api/main.py — the FastAPI app (SPEC §12.5). UI Step 1.

Mounts the three routers under /api and exposes a health check. The pipeline is
imported, never shelled out (RFI entry 15/16). A single process-wide SessionStore
is shared via app.state so every router and the SSE stream see the same sessions.

Run (compose): uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import archive
from api.routers import corpus, full_mode, hitl, job_radar, runs
from api.session import SessionStore
from tailor import telemetry

logger = logging.getLogger("cv-tailor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup retention sweep (§12.9 / D-40): delete stale private runs once at boot — but
    ONLY when RUN_RETENTION_DAYS is set, so dev and the test client never delete real runs.
    Also initialise Langfuse once (no-op unless LANGFUSE_PUBLIC_KEY is set)."""
    telemetry.init_langfuse()
    days = archive.retention_days_env()
    if days:
        removed = archive.cleanup_runs(runs.OUTPUT_DIR, days)
        if removed:
            logger.info("startup retention sweep removed %d run(s): %s", len(removed), removed)
    yield


app = FastAPI(title="cv-tailor", version="0.1.0", lifespan=lifespan,
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


@app.get("/api/debug/trace")
def debug_trace() -> dict:
    """Zero-cost Langfuse path check (F-53): create a minimal `debug_trace` + `debug_score`
    with NO LLM call and flush, then report {trace_id, enabled, host}. Use it to confirm the
    SDK→server export works without running the pipeline — then look for the id in the UI."""
    return telemetry.debug_trace()


app.include_router(corpus.router)
app.include_router(runs.router)
app.include_router(hitl.router)
app.include_router(full_mode.router)
app.include_router(job_radar.router)
