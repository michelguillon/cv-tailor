"""api/job_radar.py — server-side fetch of a JD from Job Radar (Integration §5.2, F-50).

cv-tailor can be opened from Job Radar with `?source=job_radar&job_id=<id>`; the JD is then
pulled from Job Radar's **public** `GET /api/jobs/{job_id}` and used to start a run. The fetch
is done **server-side** (here), never from the browser: it avoids CORS, keeps the one Job Radar
call in a single place, and leaves room for auth later (Phase 3/4) without touching the frontend.

Fail loud, never silent (Integration §5.2): any fetch problem raises `JobRadarError`, which the
caller turns into an error response — a run is never started with an empty/placeholder JD.
"""

from __future__ import annotations

import logging
import os

import httpx

__all__ = ["JobRadarError", "api_url", "fetch_job", "job_radar_source",
           "service_key", "cv_tailor_base_url", "post_results_to_job_radar"]

log = logging.getLogger("cv_tailor.job_radar")

DEFAULT_API_URL = "https://job-radar.michel-portfolio.co.uk"
DEFAULT_CV_TAILOR_BASE_URL = "https://cv-tailor.michel-portfolio.co.uk"


class JobRadarError(Exception):
    """Could not fetch a usable job from Job Radar (network error, 404, or bad payload)."""


def api_url() -> str:
    """Job Radar's base URL — `JOB_RADAR_API_URL` (override for local testing) or the default."""
    return os.environ.get("JOB_RADAR_API_URL", DEFAULT_API_URL).strip().rstrip("/") or DEFAULT_API_URL


def fetch_job(job_id: str, *, timeout: float = 10.0) -> dict:
    """Fetch one job's detail from Job Radar's public endpoint, or raise `JobRadarError`.

    No auth — the endpoint is public (Integration §8). The returned dict is Job Radar's raw
    JSON (company, title, source_url, fit_label, fit_score, raw_text, …)."""
    url = f"{api_url()}/api/jobs/{job_id}"
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:                      # network error, timeout, DNS, …
        raise JobRadarError(f"could not reach Job Radar ({exc})") from exc
    if resp.status_code == 404:
        raise JobRadarError(f"job {job_id!r} not found in Job Radar")
    if resp.status_code != 200:
        raise JobRadarError(f"Job Radar returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise JobRadarError("Job Radar returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise JobRadarError("Job Radar returned an unexpected payload")
    return data


def job_radar_source(data: dict) -> dict:
    """The immutable reference persisted on the run (Integration §5.2). A snapshot of the
    originating role — links back to it and tells Phase 3 where to POST the callback later."""
    return {
        "job_id": data.get("job_id"),
        "company": data.get("company"),
        "title": data.get("title"),
        "source_url": data.get("source_url"),
        "fit_label": data.get("fit_label"),
        "fit_score": data.get("fit_score"),
    }


# --------------------------------------------------------------------------- #
# Phase 3 — completed-run callback (cv-tailor → Job Radar, Integration §6)     #
# --------------------------------------------------------------------------- #

def service_key() -> str:
    """The shared secret for the Job Radar callback (`JOB_RADAR_SERVICE_KEY`), or "" when unset.

    Unset ⇒ the callback is skipped silently and the run behaves exactly as in Phase 2 — the
    integration is opt-in *by configuration*, not by a code change (Integration §6.2 / F-52)."""
    return os.environ.get("JOB_RADAR_SERVICE_KEY", "").strip()


def cv_tailor_base_url() -> str:
    """Public base URL of this cv-tailor deployment, for the `output_link` sent to Job Radar."""
    raw = os.environ.get("CV_TAILOR_BASE_URL", DEFAULT_CV_TAILOR_BASE_URL).strip().rstrip("/")
    return raw or DEFAULT_CV_TAILOR_BASE_URL


def post_results_to_job_radar(job_id: str, run_id: str, *, fit_score, coverage_score,
                              cv_quality_score, cvcm_enabled: bool, tailoring_mode,
                              output_link: str, rerun_of: str | None = None,
                              timeout: float = 5.0) -> bool:
    """POST completed-run metrics back to Job Radar (Integration §6.2). Fire-and-forget:
    **never raises** — Job Radar is not in cv-tailor's critical path. Returns True iff Job Radar
    accepted (2xx); False on a missing key, network error, timeout, or non-2xx (logged).

    Synchronous `httpx` (mirrors `fetch_job`): the run completes on a worker thread with no event
    loop, so there is no async seam to schedule onto — a sync POST after `run_complete` is the
    consistent, simplest bridge (F-52). The metric field names/scales match Job Radar's schema:
    `fit_score`/`coverage_score` are 0–1, `cv_quality_score` is 0–10 (deviation 43)."""
    key = service_key()
    if not key:                                          # opt-in by config — Phase-2 behaviour
        return False
    payload = {
        "job_id": job_id,
        "cv_tailor_run_id": run_id,
        "fit_score": fit_score,
        "coverage_score": coverage_score,
        "cv_quality_score": cv_quality_score,
        "cvcm_enabled": cvcm_enabled,
        "tailoring_mode": tailoring_mode,
        "output_link": output_link,
        "source": "cv_tailor_api",
        # Lineage (SPEC_RERUN §5): null for a fresh run, the original run_id for a re-run. Optional
        # — Job Radar can display it to show a re-tailoring, or ignore it without breaking.
        "rerun_of": rerun_of,
    }
    url = f"{api_url()}/api/cv-tailor-results"
    try:
        resp = httpx.post(url, json=payload,
                          headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except httpx.HTTPError as exc:                        # network error, timeout, DNS, …
        log.warning("Job Radar callback failed for run %s: %s", run_id, exc)
        return False
    if resp.status_code // 100 != 2:
        log.warning("Job Radar callback for run %s returned HTTP %s", run_id, resp.status_code)
        return False
    return True
