"""api/routers/job_radar.py — server-side proxy of Job Radar's public job endpoint.

Phase 2 prefill (Integration §5.2): when cv-tailor's Run page opens with
`?source=job_radar&job_id=<id>`, the frontend fetches the job *through this proxy* (not Job
Radar directly) to pre-populate the JD textarea + company field. Going server-side avoids CORS
and keeps the single Job Radar call in one place. Public — Job Radar's endpoint is itself public.

This is display-only: the authoritative fetch (and the stored reference) happens at run creation
in `runs.py`. A failure here is non-fatal — the frontend falls back to manual paste.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.job_radar import JobRadarError, fetch_job, job_radar_assessment

router = APIRouter(prefix="/api/job-radar", tags=["job-radar"])


@router.get("/jobs/{job_id}")
def prefill_job(job_id: str) -> dict:
    """Proxy Job Radar's `GET /api/jobs/{job_id}` → the fields the Run page pre-fills with."""
    try:
        job = fetch_job(job_id)
    except JobRadarError as exc:
        raise HTTPException(status_code=502, detail=f"Could not load job from Job Radar: {exc}")
    return {
        "job_id": job.get("job_id", job_id),
        "company": job.get("company"),
        "title": job.get("title"),
        "raw_text": job.get("raw_text") or "",
        "source_url": job.get("source_url"),
        "fit_label": job.get("fit_label"),
        "fit_score": job.get("fit_score"),
        # Owner's assessment context (SPEC §12.12), serialised — drives the RunPage collapsible
        # panel (owner-unlocked only; the panel itself gates visibility). None when Job Radar
        # omits it. `fetch_job` parses the typed model, so derive from the raw response dict.
        "assessment": job_radar_assessment(job),
    }
