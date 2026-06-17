"""Phase 3 — cv-tailor → Job Radar completion callback (Integration §6, F-52).

Unit-level tests for the callback function and the metric extraction. End-to-end tests
(callback fires/skips through POST /api/runs + the SSE indicator) live in test_api.py.
No real network: httpx.post is mocked or the call is exercised against on-disk fixtures.
"""

import json

import httpx
import pytest

from api import job_radar, runner

# --------------------------------------------------------------------------- #
# Phase 4 Step 2 — assessment-context enrichment (SPEC §12.12)                  #
# --------------------------------------------------------------------------- #

_JR_ASSESSMENT = {
    "fit_label": "strong_fit",
    "fit_score": 10,
    "priority_score": 9,
    "blocking_constraints": [],
    "requirement_gaps": ["formal management consulting background"],
    "fit_override": {"label": "good_fit", "reason": "requires formal consulting track record"},
    "owner_status": "shortlisted",
    "annotations": [
        {"type": "technical_depth_incorrect", "field": "technical_depth",
         "reason": "role is more strategic than technical"},
    ],
    "notes": [{"ts": "2026-06-14T18:20:00Z", "text": "Strong culture fit but consulting a gap"}],
}

_JR_EXTRACTION = {
    "role_type": ["AI Delivery"], "seniority": "director", "domain": ["AI Platform"],
    "technical_depth": "hybrid", "delivery_motion": ["enterprise_platform"],
    "required_technologies": ["python"], "required_competencies": ["leadership"],
    "requirement_gaps": ["formal management consulting background"],
    "an_unknown_future_field": "ignored",          # forward-compat: dropped, never raises
}


def _job(**overrides) -> dict:
    job = {"job_id": "sha256:abc123", "company": "Writer", "raw_text": "JD body",
           "assessment": dict(_JR_ASSESSMENT), "extraction": dict(_JR_EXTRACTION)}
    job.update(overrides)
    return job


def _mock_get(monkeypatch, payload):
    """Point job_radar.httpx.get at a 200 response carrying `payload` (no real network)."""
    monkeypatch.setattr(job_radar.httpx, "get",
                        lambda url, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", url)))


def test_fetch_job_parses_assessment(monkeypatch):
    _mock_get(monkeypatch, _job())
    a = job_radar.fetch_job("sha256:abc123")["assessment"]
    assert isinstance(a, job_radar.JobRadarAssessment)
    assert a.fit_label == "strong_fit" and a.fit_score == 10 and a.priority_score == 9
    assert a.owner_status == "shortlisted"
    assert a.requirement_gaps == ["formal management consulting background"]
    assert len(a.annotations) == 1 and a.annotations[0].field == "technical_depth"
    assert len(a.notes) == 1 and a.notes[0].text.startswith("Strong culture fit")


def test_fetch_job_parses_fit_override(monkeypatch):
    _mock_get(monkeypatch, _job())
    a = job_radar.fetch_job("sha256:abc123")["assessment"]
    assert isinstance(a.fit_override, job_radar.JobRadarFitOverride)
    assert a.fit_override.label == "good_fit"
    assert a.fit_override.reason == "requires formal consulting track record"


def test_fetch_job_parses_extraction(monkeypatch):
    _mock_get(monkeypatch, _job())
    e = job_radar.fetch_job("sha256:abc123")["extraction"]
    assert isinstance(e, job_radar.JobRadarExtraction)
    assert e.seniority == "director" and e.technical_depth == "hybrid"
    assert e.role_type == ["AI Delivery"]
    assert not hasattr(e, "an_unknown_future_field")        # unknown keys dropped, no crash


def test_fetch_job_no_assessment_returns_none(monkeypatch):
    _mock_get(monkeypatch, _job(assessment=None, extraction=None))
    job = job_radar.fetch_job("sha256:abc123")
    assert job["assessment"] is None                         # absent → None, no exception
    assert job["company"] == "Writer"                        # raw fields preserved


def test_fetch_job_no_extraction_returns_none(monkeypatch):
    # `extraction` key absent entirely (old Job Radar) → None, not a KeyError.
    _mock_get(monkeypatch, {"job_id": "x", "company": "Writer", "raw_text": "JD"})
    job = job_radar.fetch_job("x")
    assert job["assessment"] is None and job["extraction"] is None


def test_job_radar_assessment_serialises_to_plain_dict(monkeypatch):
    """The run_meta helper takes the RAW response dict and returns a JSON-ready plain dict."""
    d = job_radar.job_radar_assessment(_job())
    assert isinstance(d, dict) and d["fit_label"] == "strong_fit"
    assert d["fit_override"] == {"label": "good_fit",
                                 "reason": "requires formal consulting track record"}
    assert d["annotations"][0]["field"] == "technical_depth"
    # round-trips through JSON (no dataclass instances leak into the sidecar)
    assert json.loads(json.dumps(d))["owner_status"] == "shortlisted"
    assert job_radar.job_radar_assessment({"company": "x"}) is None    # no assessment → None


def test_serialise_helpers_idempotent_on_fetch_job_result(monkeypatch):
    """Regression: `fetch_job` embeds parsed dataclasses, and both prefill + start_run call the
    serialise helpers on that result. The helpers must tolerate an already-parsed instance — not
    re-`.get()` a dataclass (which 500'd every Job Radar JD link)."""
    _mock_get(monkeypatch, _job())
    job = job_radar.fetch_job("sha256:abc123")                  # assessment/extraction are dataclasses
    assert isinstance(job["assessment"], job_radar.JobRadarAssessment)
    a = job_radar.job_radar_assessment(job)                     # the path that used to raise
    assert isinstance(a, dict) and a["fit_label"] == "strong_fit"
    e = job_radar.job_radar_extraction(job)
    assert isinstance(e, dict) and e["seniority"] == "director"
    # and parse_* themselves are idempotent (return the same instance, no double-parse)
    assert job_radar.parse_assessment(job) is job["assessment"]
    assert job_radar.parse_extraction(job) is job["extraction"]


# --------------------------------------------------------------------------- #
# post_results_to_job_radar — payload, auth, fail-soft                          #
# --------------------------------------------------------------------------- #

def _capture_post(monkeypatch):
    """Replace job_radar.httpx.post with a recorder; returns the list it appends (url, kwargs)."""
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(job_radar.httpx, "post", fake_post)
    return calls


def test_callback_payload_correct(monkeypatch):
    monkeypatch.setenv("JOB_RADAR_SERVICE_KEY", "secret")
    monkeypatch.setenv("JOB_RADAR_API_URL", "https://jr.example")
    calls = _capture_post(monkeypatch)

    ok = job_radar.post_results_to_job_radar(
        "sha256:abc123", "run_20260612_001",
        fit_score=0.56, coverage_score=0.35, cv_quality_score=8.1,
        cvcm_enabled=True, tailoring_mode="full",
        output_link="https://cv-tailor.example/runs/run_20260612_001")
    assert ok is True and len(calls) == 1
    url, kw = calls[0]
    assert url == "https://jr.example/api/cv-tailor-results"
    assert kw["headers"]["Authorization"] == "Bearer secret"
    p = kw["json"]
    # correct field NAMES (not the old cv_tailor_score / grounding_score) and scales
    assert p["fit_score"] == 0.56 and p["coverage_score"] == 0.35
    assert p["cv_quality_score"] == 8.1                       # 0–10, raw
    assert p["cv_tailor_run_id"] == "run_20260612_001"
    assert p["source"] == "cv_tailor_api" and p["cvcm_enabled"] is True
    assert "cv_tailor_score" not in p and "grounding_score" not in p


def test_callback_skipped_without_key(monkeypatch):
    monkeypatch.delenv("JOB_RADAR_SERVICE_KEY", raising=False)
    calls = _capture_post(monkeypatch)
    ok = job_radar.post_results_to_job_radar(
        "j", "r", fit_score=0.5, coverage_score=0.5, cv_quality_score=7.0,
        cvcm_enabled=False, tailoring_mode="demo", output_link="x")
    assert ok is False and calls == []                        # opt-in by config: no POST at all


def test_callback_non_2xx_returns_false_no_raise(monkeypatch):
    monkeypatch.setenv("JOB_RADAR_SERVICE_KEY", "secret")
    monkeypatch.setattr(job_radar.httpx, "post", lambda url, **kw: httpx.Response(500))
    ok = job_radar.post_results_to_job_radar(
        "j", "r", fit_score=0.5, coverage_score=0.5, cv_quality_score=7.0,
        cvcm_enabled=False, tailoring_mode="demo", output_link="x")
    assert ok is False                                        # logged, never raised


def test_callback_network_error_returns_false_no_raise(monkeypatch):
    monkeypatch.setenv("JOB_RADAR_SERVICE_KEY", "secret")

    def boom(url, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(job_radar.httpx, "post", boom)
    ok = job_radar.post_results_to_job_radar(
        "j", "r", fit_score=0.5, coverage_score=0.5, cv_quality_score=7.0,
        cvcm_enabled=False, tailoring_mode="demo", output_link="x")
    assert ok is False                                        # network failure is swallowed


# --------------------------------------------------------------------------- #
# metric extraction from on-disk checkpoints                                   #
# --------------------------------------------------------------------------- #

def _iter_file(run_dir, n, critique_score, sections=None):
    (run_dir / f"iteration_{n}.json").write_text(json.dumps({
        "iteration": n, "keyword_coverage": 0.5, "critique_score": critique_score,
        "keyword_delta": 0.0, "quality_delta": 0.0, "sections_converged": 0,
        "sections_active": 1, "section_scores": sections or {}}), encoding="utf-8")


def test_cv_quality_score_extraction(tmp_path):
    """cv_quality_score = the latest iteration's aggregate critique_score (the Scores-tab quality),
    walking back past a fully-frozen final iteration whose score is None."""
    rd = tmp_path / "run_x"
    rd.mkdir()
    _iter_file(rd, 1, 7.2)
    _iter_file(rd, 2, 8.16)
    _iter_file(rd, 3, None)                                   # final iteration fully frozen → None
    assert runner._final_cv_quality(rd) == 8.2                # last non-None, rounded to 1dp
    # no iteration files → None, not a crash
    assert runner._final_cv_quality(tmp_path / "empty") is None


def test_callback_metrics_from_checkpoints(tmp_path):
    rd = tmp_path / "run_y"
    rd.mkdir()
    (rd / "phase1_fit_assessment.json").write_text(json.dumps({
        "outcome": "partial", "overall_fit_score": 0.56,
        "value_alignment_notes": "why you fit"}), encoding="utf-8")
    _iter_file(rd, 1, 8.1)
    m = runner._callback_metrics(rd, {"grounded_coverage": 0.35, "mode": "full"})
    assert m == {"fit_score": 0.56, "coverage_score": 0.35, "cv_quality_score": 8.1,
                 "cvcm_enabled": True, "tailoring_mode": "full"}


def test_callback_metrics_degrade_to_none(tmp_path):
    """Missing checkpoints → None metrics + cvcm False, never an exception (don't block callback)."""
    rd = tmp_path / "run_z"
    rd.mkdir()
    m = runner._callback_metrics(rd, {})
    assert m["fit_score"] is None and m["cv_quality_score"] is None
    assert m["coverage_score"] is None and m["cvcm_enabled"] is False
