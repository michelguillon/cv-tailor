"""UI Step 1 — FastAPI scaffold + session management (no API calls).

Covers the health/route shape via Starlette's TestClient and the cross-thread
session primitives (event buffer + seq, TTL cleanup, the HITL thread handoff) that
the async SSE/HITL flow (UI Steps 3–4) is built on.
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import api.routers.corpus as corpus_router
import api.routers.runs as runs_router
from api.main import app
from api.session import Session, SessionError, SessionStore


@pytest.fixture
def client():
    return TestClient(app)


def _fake_sections():
    f = "CV_Michel_Guillon_2026_AI.docx"

    def s(sid, stype, *, static=False, pos=0, title="", wc=10):
        return {"section_id": sid, "section_type": stype, "filename": f,
                "cv_type": "job_specific", "target_role": "Solutions Engineer",
                "seniority": "director", "version_date": "2026-01",
                "word_count": wc, "static": static, "title": title, "position": pos}

    return [s("profile", "profile", pos=1), s("header", "header", static=True, pos=0),
            s("experience_ms", "experience", pos=2, title="Microsoft")]


@pytest.fixture
def corpus_patched(monkeypatch):
    """Stand in for ChromaDB so corpus endpoints run without a collection."""
    monkeypatch.setattr(corpus_router, "all_sections", lambda *a, **k: _fake_sections())
    monkeypatch.setattr(corpus_router, "collection_stats",
                        lambda *a, **k: {"total": 3,
                                         "by_section_type": {"header": 1, "profile": 1, "experience": 1}})


# --------------------------------------------------------------------------- #
# app + route shape                                                           #
# --------------------------------------------------------------------------- #

def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["service"] == "cv-tailor"


def test_list_runs_is_a_list(client):
    r = client.get("/api/runs")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_unknown_run_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_hitl_unknown_run_404(client):
    # UI Step 4: the route is live; an unknown run is a 404, a missing body is a 422.
    assert client.post("/api/runs/x/hitl", json={"action": "proceed"}).status_code == 404
    assert client.post("/api/runs/x/hitl").status_code == 422


def test_stream_unknown_run_404(client):
    assert client.get("/api/runs/does-not-exist/stream").status_code == 404


# --------------------------------------------------------------------------- #
# corpus management (UI Step 2) — ChromaDB faked                               #
# --------------------------------------------------------------------------- #

def test_corpus_stats(client, corpus_patched):
    r = client.get("/api/corpus/stats")
    assert r.status_code == 200
    b = r.json()
    assert b["cv_count"] == 1 and b["section_count"] == 3
    assert b["by_section_type"]["experience"] == 1 and b["last_ingested"] == "2026-01"


def test_corpus_cvs_inventory_ordered(client, corpus_patched):
    r = client.get("/api/corpus/cvs")
    assert r.status_code == 200
    cvs = r.json()
    assert len(cvs) == 1
    cv = cvs[0]
    assert cv["filename"].endswith("AI.docx") and cv["section_count"] == 3
    assert cv["target_role"] == "Solutions Engineer" and cv["seniority"] == "director"
    assert [s["section_id"] for s in cv["sections"]] == ["header", "profile", "experience_ms"]  # by position
    assert cv["sections"][0]["static"] is True


# --------------------------------------------------------------------------- #
# run initiation + SSE progress (UI Step 3) — pipeline mocked                  #
# --------------------------------------------------------------------------- #

@pytest.fixture
def run_store(tmp_path):
    """Isolate the app's SessionStore in a tmp dir for run tests."""
    prev = app.state.sessions
    app.state.sessions = SessionStore(base_dir=tmp_path / "sessions")
    try:
        yield app.state.sessions
    finally:
        app.state.sessions = prev


def _fake_run_pipeline(jd_path, *, on_event=None, run_id=None, **kw):
    on_event({"type": "phase_start", "phase": "phase0_jd_analysis", "label": "JD analysis"})
    on_event({"type": "phase_complete", "phase": "phase0_jd_analysis", "role_title": "Director, SE"})
    on_event({"type": "run_complete", "run_id": run_id, "outcome": "partial",
              "cost_estimated_usd": 0.1, "iterations": 1})
    return {"run_id": run_id, "mode": kw.get("mode", "demo"), "outcome": "partial",
            "cost_estimated_usd": 0.1, "iterations": 1}


def _await_terminal(client, run_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/runs/{run_id}").json()
        if s["status"] in ("complete", "error", "stopped"):
            return s
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not terminate")


def test_start_run_launches_and_completes(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pipeline)
    r = client.post("/api/runs", json={"jd_text": "tailor my cv", "mode": "demo"})
    assert r.status_code == 201
    rid = r.json()["run_id"]
    s = _await_terminal(client, rid)
    assert s["status"] == "complete" and s["result"]["outcome"] == "partial"


def test_stream_replays_progress_events(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pipeline)
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo"}).json()["run_id"]
    _await_terminal(client, rid)
    with client.stream("GET", f"/api/runs/{rid}/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "phase_start" in body and "run_complete" in body and "Director, SE" in body


def test_start_run_full_mode_fails_closed_when_unconfigured(client, run_store, monkeypatch):
    # D-38: full mode is gated on the capability cookie now; with no FULL_MODE_KEY on the
    # server it fails closed with 403 (was 400 key-in-body before the unlock gate).
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    assert client.post("/api/runs", json={"jd_text": "x", "mode": "full"}).status_code == 403


def test_start_run_empty_jd_rejected(client, run_store):
    assert client.post("/api/runs", json={"jd_text": "   ", "mode": "demo"}).status_code == 400


# --------------------------------------------------------------------------- #
# conversational HITL (UI Step 4) — SSEHITL handoff through the API, no LLM     #
# --------------------------------------------------------------------------- #

def _await_status(client, run_id, status, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/runs/{run_id}").json()
        if s["status"] == status:
            return s
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never reached {status!r}")


def _fake_run_pauses_at_fit(jd_path, *, on_event=None, run_id=None, hitl=None, **kw):
    """Drive the real SSEHITL.fit handshake: publish the fit checkpoint, block on the
    human's decision, then complete. No provider is touched (button path, no free text)."""
    from types import SimpleNamespace
    on_event({"type": "phase_start", "phase": "phase1_fit_assessment", "label": "Fit assessment"})
    fit = SimpleNamespace(outcome="partial", overall_fit_score=0.6, no_fit_reason=None,
                          skills_transferable=["cloud"], gaps=[], recommended_sections={})
    jd = SimpleNamespace(role_title="Director, SE", company_context="Acme")
    proceed = hitl.fit(fit, jd)                       # blocks until POST /hitl
    outcome = "partial" if proceed else "stopped_by_human"
    on_event({"type": "run_complete", "run_id": run_id, "outcome": outcome,
              "cost_estimated_usd": 0.0, "iterations": 0})
    return {"run_id": run_id, "outcome": outcome, "cost_estimated_usd": 0.0, "iterations": 0}


def test_hitl_pause_then_resume_on_post(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pauses_at_fit)
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo", "auto": False}).json()["run_id"]

    sess = _await_status(client, rid, "awaiting_hitl")
    assert sess["hitl_pending"]["checkpoint"] == "fit_assessment"
    assert sess["hitl_pending"]["payload"]["role_title"] == "Director, SE"

    r = client.post(f"/api/runs/{rid}/hitl", json={"action": "proceed"})
    assert r.status_code == 200 and r.json()["ok"] is True

    s = _await_terminal(client, rid)
    assert s["status"] == "complete" and s["result"]["outcome"] == "partial"


def test_hitl_stop_decision_stops_the_run(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pauses_at_fit)
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo", "auto": False}).json()["run_id"]
    _await_status(client, rid, "awaiting_hitl")

    assert client.post(f"/api/runs/{rid}/hitl", json={"action": "stop"}).status_code == 200
    s = _await_terminal(client, rid)
    assert s["result"]["outcome"] == "stopped_by_human"


def test_hitl_submit_when_not_awaiting_is_409(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pipeline)   # never pauses
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo", "auto": True}).json()["run_id"]
    _await_terminal(client, rid)
    assert client.post(f"/api/runs/{rid}/hitl", json={"action": "proceed"}).status_code == 409


def test_auto_run_does_not_pause(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pauses_at_fit)
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo", "auto": True}).json()["run_id"]
    s = _await_terminal(client, rid)                 # AutoHITL.fit proceeds without a POST
    assert s["status"] == "complete" and s["result"]["outcome"] == "partial"


def test_hitl_ready_is_streamed_then_run_resumes(client, run_store, monkeypatch):
    """End-to-end through HTTP: the SSE stream delivers hitl_ready while paused; a
    concurrent POST /hitl resumes the run and the stream then carries run_complete."""
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pauses_at_fit)
    rid = client.post("/api/runs", json={"jd_text": "x", "mode": "demo", "auto": False}).json()["run_id"]

    def resume():
        _await_status(client, rid, "awaiting_hitl")
        client.post(f"/api/runs/{rid}/hitl", json={"action": "proceed"})

    t = threading.Thread(target=resume)
    t.start()
    with client.stream("GET", f"/api/runs/{rid}/stream") as resp:    # closes once the run is terminal
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    t.join(timeout=3)
    assert "hitl_ready" in body and "fit_assessment" in body and "run_complete" in body


def test_sse_hitl_review_freetext_preview_then_apply(monkeypatch):
    """Free text → Haiku interpret (mocked) → re-published as a preview → confirm-apply.
    Proves preview-before-apply: the interpretation is visible before the revision runs."""
    from types import SimpleNamespace

    from api.runner import SSEHITL
    from tailor.phases import phase4_hitl

    result = SimpleNamespace(
        manifest={"profile": {"static": False, "version": 1, "label": "Profile"}},
        iterations=[], unresolved={}, convergence_reason="converged")
    monkeypatch.setattr(phase4_hitl, "interpret_freetext",
                        lambda text, res, **k: {"section_id": "profile", "instruction": "make it punchier"})
    calls = []
    monkeypatch.setattr(phase4_hitl, "revise_section",
                        lambda sid, instr, *a, **k: (calls.append((sid, instr)), (2, "new"))[1])

    s = Session(run_id="r")
    rc = SimpleNamespace(max_iterations=1, orchestrator_model="m", validation_model="v")
    handler = SSEHITL(s, validation_model="v")
    t = threading.Thread(target=lambda: handler.review(None, result, None, None, None, rc))
    t.start()

    _spin_until(lambda: s.status == "awaiting_hitl")
    s.submit_hitl({"action": "interpret", "text": "punchier please"})

    # loop re-publishes a checkpoint carrying the pending interpretation (no revision yet)
    _spin_until(lambda: s.status == "awaiting_hitl" and s.hitl_pending["payload"].get("preview"))
    assert calls == []                                       # nothing applied during preview
    preview = s.hitl_pending["payload"]["preview"]
    assert preview == {"section_id": "profile", "instruction": "make it punchier", "label": "Profile"}

    s.submit_hitl({"action": "apply_freetext", "section_id": "profile", "instruction": "make it punchier"})
    _spin_until(lambda: any(e["type"] == "hitl_applied" for e in s.events))
    _spin_until(lambda: s.status == "awaiting_hitl")
    s.submit_hitl({"action": "accept"})

    t.join(timeout=2)
    assert calls == [("profile", "make it punchier")]


def test_sse_hitl_formatting_approve_and_reject():
    """The formatting checkpoint is binary: approve → apply (True), reject → skip (False)."""
    from api.runner import SSEHITL

    corrections = {"profile": {"corrections": ["en-dash → em-dash"], "original": "a", "corrected": "b"}}
    length = {"total_words": 100, "budget_words": 120, "over_budget": False, "longest": []}

    for action, expected in (("approve", True), ("reject", False)):
        s = Session(run_id="r")
        handler = SSEHITL(s, validation_model="v")
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("v", handler.formatting(corrections, length)))
        t.start()
        _spin_until(lambda: s.status == "awaiting_hitl")
        assert s.hitl_pending["payload"]["corrections"][0]["section_id"] == "profile"
        s.submit_hitl({"action": action})
        t.join(timeout=2)
        assert out["v"] is expected


# --------------------------------------------------------------------------- #
# archive / replay + output (UI Step 5) — fake outputs/ on disk                #
# --------------------------------------------------------------------------- #

@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    rd = out / "run_demo"
    rd.mkdir(parents=True)
    (rd / "run_log.jsonl").write_text(
        "\n".join([
            json.dumps({"phase": "phase0", "event": "jd_analysed", "reasoning": "Director, SE"}),
            json.dumps({"type": "run_complete", "mode": "demo", "iterations_run": 1,
                        "total_estimated_usd": 0.1, "grounded_coverage": 0.36,
                        "fabrication_flags": 1,
                        "cost_breakdown_estimated_usd": {"anthropic_haiku": 0.1}}),
        ]) + "\n", encoding="utf-8")
    (rd / "phase0_jd_analysis.json").write_text(json.dumps({"role_title": "Director, SE"}), encoding="utf-8")
    (rd / "phase1_fit_assessment.json").write_text(
        json.dumps({"outcome": "partial", "overall_fit_score": 0.58}), encoding="utf-8")
    (rd / "iteration_1.json").write_text(json.dumps({"iteration": 1, "keyword_coverage": 0.9}), encoding="utf-8")
    (rd / "cv_final.md").write_text("# CV\nclean cv text", encoding="utf-8")
    (rd / "cv_final.html").write_text("<html><body>full report</body></html>", encoding="utf-8")
    monkeypatch.setattr(runs_router, "OUTPUT_DIR", str(out))
    return out


def test_archive_lists_completed_runs(client, archive_dir):
    runs = client.get("/api/runs/archive").json()
    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == "run_demo" and r["role_title"] == "Director, SE"
    assert r["outcome"] == "partial" and r["cost_estimated_usd"] == 0.1
    assert r["has_md"] and r["has_html"]


def test_run_detail_replay_payload(client, archive_dir):
    d = client.get("/api/runs/run_demo/detail").json()
    assert d["role_title"] == "Director, SE" and d["outcome"] == "partial"
    assert len(d["iteration_scores"]) == 1 and d["iteration_scores"][0]["iteration"] == 1
    assert any(e.get("event") == "jd_analysed" for e in d["reasoning"])
    assert "clean cv text" in d["cv_md"]


def test_run_report_and_download(client, archive_dir):
    rep = client.get("/api/runs/run_demo/report")
    assert rep.status_code == 200 and "full report" in rep.text
    md = client.get("/api/runs/run_demo/files/cv_final.md")
    assert md.status_code == 200 and "clean cv text" in md.text
    assert client.get("/api/runs/run_demo/files/evil.sh").status_code == 404   # not downloadable


def test_archive_exposes_summary_card_fields(client, archive_dir):
    """The archive surfaces the D-34 card numbers (grounded coverage, unsupported claims,
    derived status) from the run_complete footer — for the OutputPanel card."""
    r = client.get("/api/runs/archive").json()[0]
    assert r["grounded_coverage"] == 0.36 and r["unsupported_claims"] == 1
    # partial fit (0.58) + 1 unsupported claim → Review Required; band from fit score
    assert r["status"] == "Review Required" and r["fit_band"] == "partial"
    d = client.get("/api/runs/run_demo/detail").json()
    assert d["status"] == "Review Required" and d["grounded_coverage"] == 0.36


def test_archive_unknown_run_404(client, archive_dir):
    assert client.get("/api/runs/nope/detail").status_code == 404
    assert client.get("/api/runs/nope/report").status_code == 404


def test_corpus_delete_removes_sections(client, monkeypatch):
    class FakeCollection:
        def __init__(self):
            self.deleted_where = None

        def get(self, where):
            return {"ids": ["a", "b"]} if where.get("filename") == "CV_x.docx" else {"ids": []}

        def delete(self, where):
            self.deleted_where = where

    fc = FakeCollection()
    monkeypatch.setattr(corpus_router, "get_collection", lambda *a, **k: fc)

    r = client.delete("/api/corpus/cvs/CV_x.docx")
    assert r.status_code == 200 and r.json()["sections_removed"] == 2
    assert fc.deleted_where == {"filename": "CV_x.docx"}

    assert client.delete("/api/corpus/cvs/missing.docx").status_code == 404


# --------------------------------------------------------------------------- #
# corpus add / replace / edit (UI Step 2.1, D-36) — parse, embed, chroma faked #
# --------------------------------------------------------------------------- #

VALID_META = {
    "cv_type": "job_specific", "target_role": "Solutions Engineer",
    "target_company": "Acme", "skills_emphasis": ["AI", "pre-sales"],
    "seniority": "director", "version_date": "2026-01-01",
}


def _inventory(n=6, below=False):
    return {
        "sections": [{"section_id": f"s{i}", "section_type": "experience",
                      "word_count": 50, "static": False, "title": f"S{i}"} for i in range(n)],
        "section_count": n, "below_minimum": below, "min_sections": 4,
        "warnings": [], "empty_headers": [],
    }


@pytest.fixture
def corpus_dirs(tmp_path, monkeypatch):
    """Redirect the corpus router's on-disk dirs (data/cvs + tmp staging) into tmp."""
    cv_dir = tmp_path / "cvs"
    tmp_corpus = tmp_path / "tmp_corpus"
    cv_dir.mkdir()
    monkeypatch.setattr(corpus_router, "CV_DIR", cv_dir)
    monkeypatch.setattr(corpus_router, "TMP_CORPUS", tmp_corpus)
    return cv_dir, tmp_corpus


def test_upload_returns_inventory(client, corpus_dirs, monkeypatch):
    monkeypatch.setattr(corpus_router, "preview_upload", lambda *a, **k: _inventory(6))
    r = client.post("/api/corpus/upload",
                    files={"file": ("CV_new.docx", b"PK\x03\x04")},
                    data={"metadata": json.dumps(VALID_META), "replace": "false"})
    assert r.status_code == 200
    b = r.json()
    assert b["filename"] == "CV_new.docx" and b["section_count"] == 6
    assert b["below_minimum"] is False and b["token"] and b["replace"] is False
    # The .docx is staged (not yet in the corpus) and no sidecar exists.
    cv_dir, tmp_corpus = corpus_dirs
    assert (tmp_corpus / b["token"] / "CV_new.docx").exists()
    assert not (cv_dir / "CV_new.docx").exists()


def test_upload_409_on_duplicate_without_replace(client, corpus_dirs, monkeypatch):
    cv_dir, _ = corpus_dirs
    (cv_dir / "CV_dupe.docx").write_bytes(b"x")
    monkeypatch.setattr(corpus_router, "preview_upload", lambda *a, **k: _inventory(6))
    r = client.post("/api/corpus/upload",
                    files={"file": ("CV_dupe.docx", b"PK")},
                    data={"metadata": json.dumps(VALID_META), "replace": "false"})
    assert r.status_code == 409 and "already in the corpus" in r.json()["detail"].lower()


def test_upload_flags_below_minimum(client, corpus_dirs, monkeypatch):
    monkeypatch.setattr(corpus_router, "preview_upload", lambda *a, **k: _inventory(2, below=True))
    r = client.post("/api/corpus/upload",
                    files={"file": ("CV_thin.docx", b"PK")},
                    data={"metadata": json.dumps(VALID_META), "replace": "false"})
    assert r.status_code == 200
    b = r.json()
    assert b["below_minimum"] is True and b["section_count"] == 2


def test_upload_422_on_invalid_metadata(client, corpus_dirs, monkeypatch):
    monkeypatch.setattr(corpus_router, "preview_upload", lambda *a, **k: _inventory(6))
    bad = {**VALID_META, "seniority": "wizard"}      # out of the controlled vocabulary
    r = client.post("/api/corpus/upload",
                    files={"file": ("CV_bad.docx", b"PK")},
                    data={"metadata": json.dumps(bad), "replace": "false"})
    assert r.status_code == 422 and "seniority" in r.json()["detail"].lower()


def _stub_commit(monkeypatch, *, recorder):
    """Stub the embed/store seam + chroma + budgets so confirm runs without ChromaDB."""
    def fake_commit(docx_path, fields, config, *, replace, collection=None):
        recorder["commit"] = {"replace": replace, "filename": fields["filename"]}
        return {"sections_committed": 6, "removed": 4 if replace else 0, "replaced": replace,
                "embed_tokens": 10}
    monkeypatch.setattr(corpus_router, "commit_upload", fake_commit)
    monkeypatch.setattr(corpus_router, "get_collection", lambda *a, **k: object())
    monkeypatch.setattr(corpus_router, "derive_budgets_from_collection", lambda *a, **k: {})
    monkeypatch.setattr(corpus_router, "write_budgets",
                        lambda *a, **k: recorder.__setitem__("budgets", True))


def test_confirm_commits_writes_sidecar_and_cleans_tmp(client, corpus_dirs, monkeypatch):
    cv_dir, tmp_corpus = corpus_dirs
    rec: dict = {}
    _stub_commit(monkeypatch, recorder=rec)
    # Stage a docx as if /upload had run.
    token = "tok123"
    (tmp_corpus / token).mkdir(parents=True)
    (tmp_corpus / token / "CV_new.docx").write_bytes(b"PK")

    r = client.post("/api/corpus/confirm", json={
        "token": token, "filename": "CV_new.docx", "metadata": VALID_META, "replace": False})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["sections_committed"] == 6 and b["replaced"] is False
    # Sidecar written, .docx moved into the corpus, budgets re-derived, tmp cleaned.
    assert (cv_dir / "CV_new.yaml").exists()
    assert (cv_dir / "CV_new.docx").exists()
    assert rec.get("budgets") is True
    assert not (tmp_corpus / token).exists()


def test_confirm_replace_deletes_then_commits(client, corpus_dirs, monkeypatch):
    _, tmp_corpus = corpus_dirs
    rec: dict = {}
    _stub_commit(monkeypatch, recorder=rec)
    token = "tok456"
    (tmp_corpus / token).mkdir(parents=True)
    (tmp_corpus / token / "CV_old.docx").write_bytes(b"PK")

    r = client.post("/api/corpus/confirm", json={
        "token": token, "filename": "CV_old.docx", "metadata": VALID_META, "replace": True})
    assert r.status_code == 200
    b = r.json()
    assert b["replaced"] is True and b["removed"] == 4
    assert rec["commit"]["replace"] is True


def test_confirm_410_when_upload_expired(client, corpus_dirs):
    r = client.post("/api/corpus/confirm", json={
        "token": "gone", "filename": "CV_x.docx", "metadata": VALID_META, "replace": False})
    assert r.status_code == 410


def test_edit_metadata_patches_chroma_and_writes_sidecar(client, corpus_dirs, monkeypatch):
    cv_dir, _ = corpus_dirs
    calls: dict = {}
    monkeypatch.setattr(corpus_router, "get_collection", lambda *a, **k: object())
    monkeypatch.setattr(corpus_router, "update_cv_metadata",
                        lambda fn, fields, **k: (calls.__setitem__("fn", fn), 3)[1])

    r = client.patch("/api/corpus/cvs/CV_edit.docx/metadata", json={"metadata": VALID_META})
    assert r.status_code == 200
    b = r.json()
    assert b["sections_updated"] == 3 and calls["fn"] == "CV_edit.docx"
    assert (cv_dir / "CV_edit.yaml").exists()      # sidecar overwritten on save


def test_edit_metadata_404_when_cv_absent(client, corpus_dirs, monkeypatch):
    monkeypatch.setattr(corpus_router, "get_collection", lambda *a, **k: object())
    monkeypatch.setattr(corpus_router, "update_cv_metadata", lambda *a, **k: 0)
    r = client.patch("/api/corpus/cvs/missing.docx/metadata", json={"metadata": VALID_META})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Session: event buffer + seq                                                 #
# --------------------------------------------------------------------------- #

def test_events_get_monotonic_seq_and_replay():
    s = Session(run_id="r")
    a = s.add_event({"type": "phase_start", "phase": "phase0"})
    b = s.add_event({"type": "phase_complete", "phase": "phase0"})
    assert a["seq"] == 0 and b["seq"] == 1
    assert [e["seq"] for e in s.events_since(0, timeout=0.1)] == [0, 1]
    assert s.events_since(1, timeout=0.1)[0]["type"] == "phase_complete"


def test_events_since_times_out_empty_when_nothing_new():
    s = Session(run_id="r")                 # status 'created' (non-terminal) → waits then []
    t0 = time.monotonic()
    assert s.events_since(0, timeout=0.05) == []
    assert time.monotonic() - t0 >= 0.05


# --------------------------------------------------------------------------- #
# Session: HITL thread handoff                                                 #
# --------------------------------------------------------------------------- #

def test_hitl_handoff_unblocks_pipeline_thread():
    s = Session(run_id="r")
    captured = {}

    def pipeline():                          # stands in for the background pipeline thread
        captured["resp"] = s.wait_hitl("fit_assessment", {"outcome": "partial"})

    t = threading.Thread(target=pipeline)
    t.start()
    # spin until the pipeline thread has published the checkpoint
    for _ in range(200):
        if s.status == "awaiting_hitl":
            break
        time.sleep(0.005)
    assert s.hitl_pending == {"checkpoint": "fit_assessment", "payload": {"outcome": "partial"}}
    assert s.events[-1]["type"] == "hitl_ready"

    s.submit_hitl({"proceed": True})
    t.join(timeout=2)
    assert captured["resp"] == {"proceed": True}
    assert s.status == "running" and s.hitl_pending is None


def test_submit_hitl_without_pending_raises():
    s = Session(run_id="r")
    with pytest.raises(SessionError):
        s.submit_hitl({"proceed": True})


def _spin_until(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def test_sse_hitl_review_loop_apply_then_accept(monkeypatch):
    """SSEHITL.review is multi-turn: apply an unresolved item (revision mocked), the
    loop re-publishes the updated state, then 'accept' exits. No provider is touched."""
    from types import SimpleNamespace

    from api.runner import SSEHITL
    from tailor.phases import phase4_hitl

    item = SimpleNamespace(issue="weak verbs", severity="major", suggestion="punch it up")
    result = SimpleNamespace(
        manifest={"header": {"static": True, "version": 0, "label": "Header"},
                  "profile": {"static": False, "version": 1, "label": "Profile"}},
        iterations=[], unresolved={"profile": [item]}, convergence_reason="max_iterations")
    calls = []
    monkeypatch.setattr(phase4_hitl, "revise_section",
                        lambda sid, instr, *a, **k: (calls.append((sid, instr)), (2, "new"))[1])

    s = Session(run_id="r")
    rc = SimpleNamespace(max_iterations=1, orchestrator_model="m", validation_model="v")
    handler = SSEHITL(s, validation_model="v")
    t = threading.Thread(target=lambda: handler.review(None, result, None, None, None, rc))
    t.start()

    _spin_until(lambda: s.status == "awaiting_hitl")
    assert s.hitl_pending["payload"]["unresolved"][0]["index"] == 1
    s.submit_hitl({"action": "apply_item", "index": 1})

    _spin_until(lambda: any(e["type"] == "hitl_applied" for e in s.events))
    _spin_until(lambda: s.status == "awaiting_hitl")    # loop re-published the next checkpoint
    s.submit_hitl({"action": "accept"})

    t.join(timeout=2)
    assert not t.is_alive()
    assert calls == [("profile", "punch it up")]
    assert result.unresolved["profile"] == []          # the resolved item was dropped


# --------------------------------------------------------------------------- #
# SessionStore: registry + TTL cleanup                                         #
# --------------------------------------------------------------------------- #

def test_create_get_list_delete(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    s = store.create("r1", mode="demo")
    assert (tmp_path / "r1").is_dir()
    assert store.get("r1") is s and [x.run_id for x in store.list()] == ["r1"]
    store.delete("r1")
    assert store.get("r1") is None and not (tmp_path / "r1").exists()


def test_duplicate_create_raises(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    store.create("r")
    with pytest.raises(SessionError):
        store.create("r")


def test_cleanup_drops_only_expired_terminal_sessions(tmp_path):
    store = SessionStore(base_dir=tmp_path, ttl_seconds=10)
    done = store.create("done")
    done.set_status("complete")
    running = store.create("running")        # non-terminal → never expired

    assert store.cleanup_expired(now=done.updated_at + 5) == []          # within TTL
    removed = store.cleanup_expired(now=done.updated_at + 20)            # past TTL
    assert removed == ["done"]
    assert store.get("done") is None and not (tmp_path / "done").exists()
    assert store.get("running") is not None


# --------------------------------------------------------------------------- #
# Full Mode Unlock Gate (D-38) — capability token + unlock/lock + run gate      #
# --------------------------------------------------------------------------- #

import api.security as security  # noqa: E402


def test_capability_token_roundtrip_tamper_and_expiry(monkeypatch):
    monkeypatch.setenv("FULL_MODE_KEY", "s3cret")
    tok = security.issue_token()
    assert security.verify_token(tok)
    assert not security.verify_token(tok + "x")          # tampered signature
    assert not security.verify_token("9999999999.deadbeef")
    assert not security.verify_token(None) and not security.verify_token("")
    expired = security.issue_token(now=-security.FULL_MODE_TTL - 10)
    assert not security.verify_token(expired)            # exp in the past
    monkeypatch.setenv("FULL_MODE_KEY", "rotated")        # rotating the key invalidates it
    assert not security.verify_token(tok)


def test_key_matches_and_configured(monkeypatch):
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    assert not security.full_mode_configured() and not security.key_matches("x")
    monkeypatch.setenv("FULL_MODE_KEY", "abc")
    assert security.full_mode_configured()
    assert security.key_matches("abc") and not security.key_matches("abd")


def test_capabilities_reflects_config_and_cookie(client, monkeypatch):
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    c = client.get("/api/capabilities").json()
    assert c["demo_available"] and not c["full_configured"] and not c["full_unlocked"]
    monkeypatch.setenv("FULL_MODE_KEY", "pw")
    c = client.get("/api/capabilities").json()
    assert c["full_configured"] and not c["full_unlocked"]


def test_unlock_wrong_key_401_and_unconfigured_403(client, monkeypatch):
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    assert client.post("/api/full-mode/unlock", json={"key": "x"}).status_code == 403
    monkeypatch.setenv("FULL_MODE_KEY", "pw")
    r = client.post("/api/full-mode/unlock", json={"key": "nope"})
    assert r.status_code == 401
    assert client.get("/api/capabilities").json()["full_unlocked"] is False


def test_unlock_then_lock_cycle(client, monkeypatch):
    monkeypatch.setenv("FULL_MODE_KEY", "pw")
    assert client.post("/api/full-mode/unlock", json={"key": "pw"}).json()["unlocked"] is True
    assert client.get("/api/capabilities").json()["full_unlocked"] is True   # cookie carried
    assert client.post("/api/full-mode/lock").json()["unlocked"] is False
    assert client.get("/api/capabilities").json()["full_unlocked"] is False


def test_full_run_blocked_until_unlocked(client, run_store, monkeypatch):
    import api.runner as runner
    monkeypatch.setattr(runner, "run_pipeline", _fake_run_pipeline)
    monkeypatch.setenv("FULL_MODE_KEY", "pw")
    # no capability cookie yet → fail closed
    assert client.post("/api/runs", json={"jd_text": "x", "mode": "full"}).status_code == 403
    client.post("/api/full-mode/unlock", json={"key": "pw"})                 # sets the cookie
    r = client.post("/api/runs", json={"jd_text": "x", "mode": "full"})
    assert r.status_code == 201 and r.json()["mode"] == "full"
    # demo never needs a cookie
    assert client.post("/api/runs", json={"jd_text": "x", "mode": "demo"}).status_code == 201
