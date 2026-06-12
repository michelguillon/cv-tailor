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
