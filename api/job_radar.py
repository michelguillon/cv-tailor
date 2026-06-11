"""api/job_radar.py — server-side fetch of a JD from Job Radar (Integration §5.2, F-50).

cv-tailor can be opened from Job Radar with `?source=job_radar&job_id=<id>`; the JD is then
pulled from Job Radar's **public** `GET /api/jobs/{job_id}` and used to start a run. The fetch
is done **server-side** (here), never from the browser: it avoids CORS, keeps the one Job Radar
call in a single place, and leaves room for auth later (Phase 3/4) without touching the frontend.

Fail loud, never silent (Integration §5.2): any fetch problem raises `JobRadarError`, which the
caller turns into an error response — a run is never started with an empty/placeholder JD.
"""

from __future__ import annotations

import os

import httpx

__all__ = ["JobRadarError", "api_url", "fetch_job", "job_radar_source"]

DEFAULT_API_URL = "https://job-radar.michel-portfolio.co.uk"


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
