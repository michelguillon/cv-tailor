"""SQLite run store (SPEC_SQLITE_MIGRATION §2/§3/§4.2) — schema, write path, migration, list.

Pure stdlib (sqlite3 + json); no API calls. Synthetic run dirs under tmp_path mirror the
real on-disk shapes (run_log footer, phase0/1 checkpoints, iteration_*.json, manifest,
run_meta.json sidecar). The write path and the migration share the same disk→row builders
(F-59), so these also cover the migration's row mapping.
"""

import json

import pytest

from cli import migrate_runs, reconcile_runs
from tailor import db


# --------------------------------------------------------------------------- #
# synthetic run dirs                                                           #
# --------------------------------------------------------------------------- #

def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def make_run(out, run_id, *, footer=True, failed=False, mode="demo", outcome="partial",
             fit_score=0.6, role="Director, SE", cvcm=True, no_fit=None, meta=None,
             iterations=None, manifest=None, final_manifest=False):
    """A minimal but realistic run dir. `iterations` is a list of iteration dicts;
    `manifest` a section→meta dict. Returns the run dir Path."""
    rd = out / run_id
    rd.mkdir(parents=True)
    lines = [json.dumps({"ts": "2026-01-01T00:00:00Z", "phase": "phase0", "event": "x",
                         "reasoning": "r"})]
    if failed:
        lines.append(json.dumps({"type": "run_failed", "error": "boom"}))
    if footer:
        lines.append(json.dumps({"type": "run_complete", "mode": mode, "iterations_run":
                                 len(iterations or []), "total_estimated_usd": 0.12,
                                 "grounded_coverage": 0.5,
                                 "cost_breakdown_estimated_usd": {"anthropic_haiku": 0.12}}))
    (rd / "run_log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(rd / "phase0_jd_analysis.json", {"role_title": role})
    p1 = {"outcome": outcome, "overall_fit_score": fit_score,
          "value_alignment_notes": "aligns" if cvcm else None, "no_fit_reason": no_fit}
    _write(rd / "phase1_fit_assessment.json", p1)
    for it in (iterations or []):
        _write(rd / f"iteration_{it['iteration']}.json", it)
    if manifest is not None:
        name = "final_manifest.json" if final_manifest else "phase2_draft_manifest.json"
        _write(rd / name, manifest)
    if meta is not None:
        _write(rd / "run_meta.json", meta)
    return rd


def _iter(n, **kw):
    base = {"iteration": n, "keyword_coverage": 0.5, "critique_score": 7.5,
            "keyword_delta": 0.0, "quality_delta": 0.0, "sections_converged": 1,
            "sections_active": 2, "section_scores": {}}
    base.update(kw)
    return base


SECTION_MANIFEST = {
    "header": {"static": True, "version": None, "section_type": "header", "position": 0,
               "source_cv": "Figma"},
    "profile": {"static": False, "version": 2, "section_type": "profile", "position": 1,
                "source_cv": "Airwallex"},
}


# --------------------------------------------------------------------------- #
# db path derivation                                                           #
# --------------------------------------------------------------------------- #

def test_db_path_sibling_of_output_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    out.mkdir()
    assert db.db_path_for(out) == (tmp_path / "data" / "cv_tailor.db")


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CV_TAILOR_DB", str(tmp_path / "custom.db"))
    assert db.db_path_for("outputs") == tmp_path / "custom.db"


# --------------------------------------------------------------------------- #
# write path + row content                                                     #
# --------------------------------------------------------------------------- #

def test_record_and_query_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    scores = {"profile": {"keyword_coverage": 0.88, "claude_quality": 7.0, "gpt_quality": 8.0,
                          "selected_writer": "gpt", "converged": True}}
    make_run(out, "run_20260614_143000", mode="full", outcome="partial",
             iterations=[_iter(1, section_scores=scores)], manifest=SECTION_MANIFEST,
             final_manifest=True, meta={"public_demo": True, "keep": True,
                                        "rerun_of": "run_20260613_111401",
                                        "job_radar_source": {"job_id": "sha256:abc"}})
    ok = db.record_run_complete("run_20260614_143000", output_dir=out,
                                summary={"convergence_reason": "both_signals_plateau"})
    assert ok is True

    res = db.query_runs(out)
    assert res["total"] == 1 and res["limit"] == 20 and res["offset"] == 0
    row = res["runs"][0]
    assert row["run_id"] == "run_20260614_143000"
    assert row["mode"] == "full" and row["fit_outcome"] == "partial"
    assert row["public_demo"] is True
    assert row["job_radar_job_id"] == "sha256:abc" and row["rerun_of"] == "run_20260613_111401"
    assert row["jd_role_title"] == "Director, SE"

    # full row + child tables via a direct read
    with db.get_db(out) as conn:
        r = dict(conn.execute("SELECT * FROM runs").fetchone())
        assert r["convergence_reason"] == "both_signals_plateau"   # summary overlay
        assert r["cvcm_enabled"] == 1 and r["keep"] == 1
        assert r["coverage_score"] == 0.5 and r["quality_score"] == 7.5
        secs = {s["section_id"]: dict(s) for s in
                conn.execute("SELECT * FROM run_sections").fetchall()}
        assert secs["header"]["static"] == 1 and secs["header"]["converged"] is None
        assert secs["profile"]["static"] == 0 and secs["profile"]["converged"] == 1
        assert secs["profile"]["selected_writer"] == "gpt" and secs["profile"]["final_version"] == 2
        its = conn.execute("SELECT COUNT(*) FROM run_iterations").fetchone()[0]
        assert its == 1


def test_failed_run_recorded_with_status(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260609_144918", footer=False, failed=True)
    with db.get_db(out) as conn:
        assert db.write_run(conn, out / "run_20260609_144918")
    row = db.query_runs(out)["runs"][0]
    assert row["status"] == "failed"


def test_incomplete_run_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_x", footer=False, failed=False)        # no footer, not failed
    assert db.build_run_row(out / "run_x") is None
    assert db.record_run_complete("run_x", output_dir=out) is False
    assert db.query_runs(out)["total"] == 0


def test_no_fit_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260601_000000", outcome="no_fit", cvcm=False,
             no_fit="role requires a security clearance")
    db.record_run_complete("run_20260601_000000", output_dir=out)
    with db.get_db(out) as conn:
        r = dict(conn.execute("SELECT * FROM runs").fetchone())
    assert r["fit_outcome"] == "no_fit" and r["cvcm_enabled"] == 0
    assert r["no_fit_reason"] == "role requires a security clearance"
    assert r["convergence_reason"] is None        # no summary overlay → null


# --------------------------------------------------------------------------- #
# idempotency: REPLACE refreshes, IGNORE preserves                             #
# --------------------------------------------------------------------------- #

def test_replace_refreshes_and_prunes_children(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260606_084416", iterations=[_iter(1), _iter(2)], manifest=SECTION_MANIFEST)
    db.record_run_complete("run_20260606_084416", output_dir=out)
    with db.get_db(out) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_iterations").fetchone()[0] == 2

    # Re-run the same id with fewer iterations → REPLACE prunes the stale child row.
    import shutil
    shutil.rmtree(out / "run_20260606_084416")
    make_run(out, "run_20260606_084416", iterations=[_iter(1)], manifest=SECTION_MANIFEST)
    db.record_run_complete("run_20260606_084416", output_dir=out)
    with db.get_db(out) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_iterations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_upsert_preserves_live_only_convergence_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_a", outcome="strong", iterations=[_iter(1)])
    db.record_run_complete("run_a", output_dir=out,
                           summary={"convergence_reason": "live_value"})
    # A migration (no summary → NULL convergence_reason) must NOT clobber the live value:
    # COALESCE(excluded, existing) keeps it.
    with db.get_db(out) as conn:
        db.write_run(conn, out / "run_a")
        r = dict(conn.execute("SELECT * FROM runs").fetchone())
    assert r["convergence_reason"] == "live_value"


# --------------------------------------------------------------------------- #
# query_runs: pagination, filter, ordering                                     #
# --------------------------------------------------------------------------- #

def test_query_pagination_filter_ordering(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260601_000000", mode="demo")
    make_run(out, "run_20260602_000000", mode="full", meta={"public_demo": True})
    make_run(out, "run_20260603_000000", mode="demo")
    for rid in ("run_20260601_000000", "run_20260602_000000", "run_20260603_000000"):
        db.record_run_complete(rid, output_dir=out)

    res = db.query_runs(out)
    assert res["total"] == 3
    assert [r["run_id"] for r in res["runs"]] == [          # ts DESC
        "run_20260603_000000", "run_20260602_000000", "run_20260601_000000"]

    page = db.query_runs(out, limit=1, offset=1)
    assert page["total"] == 3 and len(page["runs"]) == 1
    assert page["runs"][0]["run_id"] == "run_20260602_000000"

    assert db.query_runs(out, mode="full")["total"] == 1
    pub = db.query_runs(out, public_only=True)
    assert pub["total"] == 1 and pub["runs"][0]["run_id"] == "run_20260602_000000"


def test_new_columns_populated_and_listed(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    # fabrication_flags on the footer → unsupported_claims; sidecar company_name resolved.
    rd = make_run(out, "run_20260701_000000", meta={"company_name": "Acme"})
    log = (rd / "run_log.jsonl").read_text(encoding="utf-8").replace(
        '"grounded_coverage": 0.5,', '"grounded_coverage": 0.5, "fabrication_flags": 3,')
    (rd / "run_log.jsonl").write_text(log, encoding="utf-8")
    db.record_run_complete("run_20260701_000000", output_dir=out)
    row = db.query_runs(out)["runs"][0]
    assert row["company_name"] == "Acme" and row["unsupported_claims"] == 3
    assert row["keep"] is False and "cost_usd" in row


def test_schema_evolution_alters_existing_db(tmp_path, monkeypatch):
    """A Phase-1-era DB (full original schema, without the §5.2 columns) gets them ALTER-ed
    in on next open — no rebuild, existing rows preserved."""
    import sqlite3
    monkeypatch.setenv("CV_TAILOR_DB", str(tmp_path / "old.db"))
    with db.get_db("outputs") as conn:                 # create the full current schema
        conn.execute("INSERT INTO runs (run_id, ts, mode, status, convergence_reason) "
                     "VALUES ('old', '2026-01-01', 'demo', 'complete', 'plateau')")
    # Simulate the deployed Phase-1 DB: same schema minus the two new columns.
    raw = sqlite3.connect(str(tmp_path / "old.db"))
    raw.execute("ALTER TABLE runs DROP COLUMN company_name")
    raw.execute("ALTER TABLE runs DROP COLUMN unsupported_claims")
    raw.commit()
    raw.close()
    with db.get_db("outputs") as conn:                 # reopen → init_schema ALTERs them back
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert "company_name" in cols and "unsupported_claims" in cols
        r = dict(conn.execute("SELECT * FROM runs WHERE run_id='old'").fetchone())
        assert r["convergence_reason"] == "plateau"    # existing row + live-only field preserved
        assert r["company_name"] is None               # new column defaults NULL until backfilled


def test_query_empty_db_no_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    out.mkdir()
    res = db.query_runs(out)        # schema created on demand; no rows
    assert res == {"runs": [], "total": 0, "limit": 20, "offset": 0}


# --------------------------------------------------------------------------- #
# migration script                                                             #
# --------------------------------------------------------------------------- #

def test_migrate_counts_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260601_000000", iterations=[_iter(1)])
    make_run(out, "run_20260602_000000", iterations=[_iter(1)])
    make_run(out, "run_incomplete", footer=False)        # skipped (no footer)

    first = migrate_runs.migrate(out)
    assert first["recorded"] == 2 and first["skipped"] == 1 and first["total"] == 3
    assert "run_incomplete" in first["skipped_ids"]
    assert db.query_runs(out)["total"] == 2

    second = migrate_runs.migrate(out)                    # idempotent
    assert second["recorded"] == 2
    assert db.query_runs(out)["total"] == 2               # no duplicates


def test_migrate_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260601_000000")
    res = migrate_runs.migrate(out, dry_run=True)
    assert res["recorded"] == 1
    assert db.query_runs(out)["total"] == 0               # nothing persisted


# --------------------------------------------------------------------------- #
# reconciliation gate (the migration soak gate)                                #
# --------------------------------------------------------------------------- #

def test_reconcile_clean_after_migration(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_20260601_000000", iterations=[_iter(1)], manifest=SECTION_MANIFEST)
    make_run(out, "run_20260602_000000", iterations=[_iter(1), _iter(2)])
    migrate_runs.migrate(out)
    r = reconcile_runs.reconcile(out)
    assert r["clean"] is True
    assert r["disk_runs"] == 2 and r["sqlite_runs"] == 2
    assert not r["missing"] and not r["orphan"] and not r["mismatches"]


def test_reconcile_flags_missing_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_a", fit_score=0.5, iterations=[_iter(1)])
    make_run(out, "run_b", fit_score=0.6)
    migrate_runs.migrate(out)
    with db.get_db(out) as conn:
        conn.execute("UPDATE runs SET fit_score=0.999 WHERE run_id='run_a'")   # corrupt a field
        conn.execute("DELETE FROM runs WHERE run_id='run_b'")                  # drop a row

    r = reconcile_runs.reconcile(out)
    assert r["clean"] is False
    assert r["missing"] == ["run_b"]
    assert any(m["field"] == "fit_score" and m["run_id"] == "run_a" for m in r["mismatches"])


def test_reconcile_public_demo_drift_is_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_a")
    migrate_runs.migrate(out)
    # Simulate a post-write PATCH that flipped the sidecar but not (yet) SQLite (Phase 1/2 reality).
    with db.get_db(out) as conn:
        conn.execute("UPDATE runs SET public_demo=1 WHERE run_id='run_a'")

    r = reconcile_runs.reconcile(out)
    assert r["clean"] is True                              # drift does not gate
    assert any(d["field"] == "public_demo" for d in r["drift"])


def test_reconcile_orphan_row(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_DB", raising=False)
    out = tmp_path / "outputs"
    make_run(out, "run_a")
    migrate_runs.migrate(out)
    import shutil
    shutil.rmtree(out / "run_a")                           # row remains, disk run gone
    r = reconcile_runs.reconcile(out)
    assert r["clean"] is False and r["orphan"] == ["run_a"]
