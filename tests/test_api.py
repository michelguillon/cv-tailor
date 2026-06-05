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


def test_hitl_route_still_stubbed(client):
    # Conversational HITL resume lands in UI Step 4.
    assert client.post("/api/runs/x/hitl").status_code == 501


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


def test_start_run_full_mode_requires_key(client, run_store, monkeypatch):
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    assert client.post("/api/runs", json={"jd_text": "x", "mode": "full"}).status_code == 400


def test_start_run_empty_jd_rejected(client, run_store):
    assert client.post("/api/runs", json={"jd_text": "   ", "mode": "demo"}).status_code == 400


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
                        "total_estimated_usd": 0.1,
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
