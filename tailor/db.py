"""tailor/db.py — SQLite run store (SPEC_SQLITE_MIGRATION §2/§3).

Structured run metadata, queryable. Complementary to — not a replacement for — the
two existing durable substrates: ``run_log.jsonl`` (the append-only audit trail, D-06)
and the per-run checkpoint files on disk (section text, phase outputs, D-07 #3). The
invariant (§1): **SQLite holds structured metadata; JSONL holds the full audit trail;
the filesystem holds binary artifacts and section text.**

Why the row builders read from disk rather than from a ``PipelineOutput``
---------------------------------------------------------------------------
SPEC_SQLITE_MIGRATION sketches ``record_run_complete(run_id, pipeline_output)``, but
this codebase never materialises a ``PipelineOutput`` at runtime — a run's data lives
in its checkpoint files (``phase0_jd_analysis.json``, ``phase1_fit_assessment.json``,
``iteration_*.json``, the draft manifest) exactly as ``api/archive.py`` reads them
(the D-07 #3 "drafts on disk, not fields on an object" convention). So the row
builders here read those same files. This makes the live write path
(``record_run_complete``, called once at ``run_complete``) and the one-time
``cli/migrate_runs.py`` backfill produce **identical** rows from the **same** source —
there is no second mapping to drift (F-59).

Two fields are not persisted to any checkpoint — ``convergence_reason`` and
``converged`` ride only the in-memory pipeline summary / the SSE ``run_complete`` event.
The live path passes them through ``summary=``; the migration leaves them NULL (an old
run simply has no value, which §7 explicitly accepts).

The run's visibility/retention sidecar (``run_meta.json``, ``api/run_meta.py``) supplies
``public_demo`` / ``keep`` / ``job_radar_job_id`` / ``rerun_of``. db.py reads that file
**by path** (it is just another file in the run dir) rather than importing ``api`` —
keeping the package layering one-way (api → tailor → db), never back.

Database location (§3): ``<project>/data/cv_tailor.db``, derived from the run
``output_dir`` (its sibling ``data/`` dir) so a test pointing ``output_dir`` at a
``tmp_path`` automatically gets an isolated DB and never writes into the repo. An
explicit ``CV_TAILOR_DB`` env var overrides the derivation when set.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "db_path_for", "get_db", "init_schema",
    "record_run_complete", "query_runs", "get_run_detail",
    "RUN_COLUMNS", "SECTION_COLUMNS", "ITERATION_COLUMNS",
]

# --------------------------------------------------------------------------- #
# Schema (§2)                                                                  #
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',
    job_radar_job_id    TEXT,
    rerun_of            TEXT,
    public_demo         INTEGER NOT NULL DEFAULT 0,
    keep                INTEGER NOT NULL DEFAULT 0,
    fit_score           REAL,
    fit_outcome         TEXT,
    coverage_score      REAL,
    quality_score       REAL,
    cvcm_enabled        INTEGER NOT NULL DEFAULT 0,
    convergence_reason  TEXT,
    iterations_run      INTEGER,
    cost_usd            REAL,
    jd_role_title       TEXT,
    value_alignment     TEXT,
    no_fit_reason       TEXT,
    company_name        TEXT,                       -- resolved display label (sidecar → JD-inferred)
    unsupported_claims  INTEGER                     -- verifier fabrication-flag count (F-35)
);

CREATE TABLE IF NOT EXISTS run_sections (
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    section_id       TEXT NOT NULL,
    section_type     TEXT NOT NULL,
    position         INTEGER NOT NULL,
    static           INTEGER NOT NULL DEFAULT 0,
    final_version    INTEGER,
    converged        INTEGER,
    keyword_coverage REAL,
    claude_quality   REAL,
    gpt_quality      REAL,
    selected_writer  TEXT,
    source_cv        TEXT,
    PRIMARY KEY (run_id, section_id)
);

CREATE TABLE IF NOT EXISTS run_iterations (
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    iteration           INTEGER NOT NULL,
    keyword_coverage    REAL,
    quality_score       REAL,
    keyword_delta       REAL,
    quality_delta       REAL,
    sections_converged  INTEGER,
    sections_active     INTEGER,
    rubric_version      INTEGER,
    PRIMARY KEY (run_id, iteration)
);

CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_runs_job_radar ON runs(job_radar_job_id) WHERE job_radar_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_public ON runs(public_demo) WHERE public_demo = 1;
"""

# Column order is the contract for the row builders below — keep it in lock-step
# with the INSERT statements (named-tuple-free: explicit lists, not SELECT *).
RUN_COLUMNS = (
    "run_id", "ts", "mode", "status", "job_radar_job_id", "rerun_of", "public_demo",
    "keep", "fit_score", "fit_outcome", "coverage_score", "quality_score", "cvcm_enabled",
    "convergence_reason", "iterations_run", "cost_usd", "jd_role_title", "value_alignment",
    "no_fit_reason", "company_name", "unsupported_claims",
)

# Columns added after the initial (deployed) schema — ALTER-ed in on demand for an existing DB
# (SPEC_SQLITE_MIGRATION Phase 2 §5.2). New additions go here, never a destructive rebuild.
_ADDED_COLUMNS = (("company_name", "TEXT"), ("unsupported_claims", "INTEGER"))
SECTION_COLUMNS = (
    "run_id", "section_id", "section_type", "position", "static", "final_version",
    "converged", "keyword_coverage", "claude_quality", "gpt_quality", "selected_writer",
    "source_cv",
)
ITERATION_COLUMNS = (
    "run_id", "iteration", "keyword_coverage", "quality_score", "keyword_delta",
    "quality_delta", "sections_converged", "sections_active", "rubric_version",
)


# --------------------------------------------------------------------------- #
# Connection                                                                   #
# --------------------------------------------------------------------------- #

def db_path_for(output_dir: str | Path = "outputs") -> Path:
    """Resolve the SQLite path for a given run ``output_dir`` (§3).

    ``CV_TAILOR_DB`` wins when set. Otherwise the DB is ``data/cv_tailor.db`` as a
    sibling of ``output_dir`` (``outputs/`` and ``data/`` sit side-by-side under the
    project root) — so production (``output_dir="outputs"``) lands in ``data/`` while
    a test pointing ``output_dir`` at a ``tmp_path`` gets an isolated, throwaway DB.
    """
    env = os.environ.get("CV_TAILOR_DB", "").strip()
    if env:
        return Path(env)
    return Path(output_dir).resolve().parent / "data" / "cv_tailor.db"


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables + indexes if absent (idempotent). WAL so the API can read while
    the pipeline's worker thread writes at run completion. Also ALTER-in any columns added
    after the initial deployed schema (§5.2) so an existing DB evolves without a rebuild."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for col, decl in _ADDED_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")


@contextmanager
def get_db(output_dir: str | Path = "outputs"):
    """Context manager yielding a schema-ensured connection; commits on clean exit,
    rolls back on exception, always closes. Row factory is ``sqlite3.Row`` for
    name-addressable reads."""
    path = db_path_for(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Disk readers (shared by the live write path and the migration)              #
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        return out
    return out


def _ts_from_run_id(run_id: str) -> str | None:
    """Parse the UTC stamp embedded in a ``run_YYYYMMDD_HHMMSS`` id → ISO 8601, or None.
    Re-implemented here (rather than importing ``api.run_meta``) to keep db.py free of an
    api back-import; the format is the run-id contract (``tailor/run_context.new_run_id``)."""
    try:
        dt = datetime.strptime(run_id, "run_%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


def _run_ts(run_dir: Path, run_id: str, log: list[dict]) -> str:
    """A non-null ts for the run: the id's stamp, else the first log entry's ts, else "".
    (``ts`` is NOT NULL and the column we sort on; an unparseable demo id falls back to a
    real audit ts so the run still sorts sensibly.)"""
    stamp = _ts_from_run_id(run_id)
    if stamp:
        return stamp
    for entry in log:
        if entry.get("ts"):
            return entry["ts"]
    return ""


def _footer(log: list[dict]) -> dict:
    """The run_complete cost footer (last one wins), or {}."""
    for entry in reversed(log):
        if entry.get("type") == "run_complete":
            return entry
    return {}


def _has_failed(log: list[dict]) -> bool:
    return any(e.get("type") == "run_failed" for e in log)


def _manifest(run_dir: Path) -> dict:
    """The final section manifest, preferring the post-refinement checkpoint (F-40);
    older runs only wrote the Phase-2 draft manifest."""
    return (_read_json(run_dir / "final_manifest.json")
            or _read_json(run_dir / "phase2_draft_manifest.json")
            or {})


def _iterations(run_dir: Path) -> list[dict]:
    """Iteration checkpoints in order."""
    paths = sorted(run_dir.glob("iteration_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    return [d for d in (_read_json(p) for p in paths) if d is not None]


def _final_section_scores(iters: list[dict]) -> dict:
    """Per-section *final* score state: the latest iteration in which each section
    appears wins (a section that froze early carries its last-scored state forward —
    same intent as ``runner._final_cv_quality`` for the run-level number)."""
    final: dict[str, dict] = {}
    for it in iters:
        for sid, score in (it.get("section_scores") or {}).items():
            final[sid] = score
    return final


def _final_quality(iters: list[dict]) -> float | None:
    """The run's overall CV quality (0–10): the last iteration with a non-null
    ``critique_score`` (a fully-converged final iteration freezes all sections and
    reports None — walk back to the real converged quality). Mirrors
    ``api/runner._final_cv_quality``."""
    for it in reversed(iters):
        score = it.get("critique_score")
        if score is not None:
            return round(score, 1)
    return None


def _sidecar(run_dir: Path) -> dict:
    """The visibility/retention sidecar (``run_meta.json``) read by path. Absent on a
    plain CLI/demo run → {} → private/not-kept/no-Job-Radar defaults."""
    return _read_json(run_dir / "run_meta.json") or {}


# --------------------------------------------------------------------------- #
# Row builders                                                                 #
# --------------------------------------------------------------------------- #

def build_run_row(run_dir: Path, *, summary: dict | None = None) -> dict | None:
    """Assemble the ``runs`` row from a run's on-disk checkpoints (+ optional in-memory
    ``summary`` overlay for ``convergence_reason``/``converged``). Returns None when the
    run never produced an audit trail (nothing to record)."""
    run_id = run_dir.name
    log = _read_jsonl(run_dir / "run_log.jsonl")
    if not log:
        return None
    footer = _footer(log)
    failed = _has_failed(log)
    if not footer and not failed:
        return None                       # incomplete run (crashed before any footer) — skip

    p0 = _read_json(run_dir / "phase0_jd_analysis.json") or {}
    p1 = _read_json(run_dir / "phase1_fit_assessment.json") or {}
    iters = _iterations(run_dir)
    meta = _sidecar(run_dir)
    summary = summary or {}

    status = "complete" if footer else "failed"
    jr = meta.get("job_radar_source") or {}
    grounded = footer.get("grounded_coverage")
    if grounded is None and iters:
        grounded = iters[-1].get("keyword_coverage")

    return {
        "run_id": run_id,
        "ts": _run_ts(run_dir, run_id, log),
        "mode": footer.get("mode") or summary.get("mode") or "demo",
        "status": status,
        "job_radar_job_id": jr.get("job_id"),
        "rerun_of": meta.get("rerun_of"),
        "public_demo": 1 if meta.get("public_demo") else 0,
        "keep": 1 if meta.get("keep") else 0,
        "fit_score": p1.get("overall_fit_score"),
        "fit_outcome": p1.get("outcome"),
        "coverage_score": grounded,
        "quality_score": _final_quality(iters),
        "cvcm_enabled": 1 if p1.get("value_alignment_notes") is not None else 0,
        "convergence_reason": summary.get("convergence_reason"),
        "iterations_run": footer.get("iterations_run", len(iters) or None),
        "cost_usd": footer.get("total_estimated_usd"),
        "jd_role_title": p0.get("role_title"),
        "value_alignment": p1.get("value_alignment_notes"),
        "no_fit_reason": p1.get("no_fit_reason"),
        # Display label (F-47 precedence: owner's manual/edited sidecar value → JD-inferred).
        "company_name": meta.get("company_name") or p0.get("company_name"),
        "unsupported_claims": footer.get("fabrication_flags"),
    }


def build_section_rows(run_dir: Path) -> list[dict]:
    """Assemble ``run_sections`` rows: structure from the manifest, scores from the
    final per-section state across iterations. Static sections carry structure only."""
    run_id = run_dir.name
    manifest = _manifest(run_dir)
    final_scores = _final_section_scores(_iterations(run_dir))
    rows = []
    for sid, m in manifest.items():
        score = final_scores.get(sid) or {}
        is_static = bool(m.get("static"))
        rows.append({
            "run_id": run_id,
            "section_id": sid,
            "section_type": m.get("section_type") or "",
            "position": m.get("position") if m.get("position") is not None else 0,
            "static": 1 if is_static else 0,
            "final_version": m.get("version"),
            "converged": None if is_static else (1 if score.get("converged") else 0) if score else None,
            "keyword_coverage": score.get("keyword_coverage"),
            "claude_quality": score.get("claude_quality"),
            "gpt_quality": score.get("gpt_quality"),
            "selected_writer": score.get("selected_writer"),
            "source_cv": m.get("source_cv"),
        })
    return rows


def build_iteration_rows(run_dir: Path) -> list[dict]:
    """Assemble ``run_iterations`` rows from the iteration checkpoints."""
    run_id = run_dir.name
    rows = []
    for it in _iterations(run_dir):
        rows.append({
            "run_id": run_id,
            "iteration": it.get("iteration"),
            "keyword_coverage": it.get("keyword_coverage"),
            "quality_score": it.get("critique_score"),
            "keyword_delta": it.get("keyword_delta"),
            "quality_delta": it.get("quality_delta"),
            "sections_converged": it.get("sections_converged"),
            "sections_active": it.get("sections_active"),
            "rubric_version": it.get("rubric_version"),   # not in the checkpoint → None
        })
    return rows


# --------------------------------------------------------------------------- #
# Write path                                                                   #
# --------------------------------------------------------------------------- #

# UPSERT: sync every disk-derived column from the incoming row, but never lose the
# live-only convergence_reason — COALESCE prefers the incoming value (live path supplies it)
# and falls back to the stored one (a migration passes NULL, so the row keeps it). This makes
# the live write and the migration idempotent AND lets a re-migration backfill columns added
# later (§5.2) on rows written before they existed.
_RUN_UPSERT = (
    f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in RUN_COLUMNS)}) "
    "ON CONFLICT(run_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in RUN_COLUMNS
                if c not in ("run_id", "convergence_reason"))
    + ", convergence_reason = COALESCE(excluded.convergence_reason, runs.convergence_reason)"
)


def _insert(conn: sqlite3.Connection, table: str, columns, rows: list[dict]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r[c] for c in columns) for r in rows])


def write_run(conn: sqlite3.Connection, run_dir: Path, *, summary: dict | None = None) -> bool:
    """Write one run's three tables through an existing connection (caller owns the
    transaction). Idempotent UPSERT: re-runs/retries and re-migrations converge to the
    on-disk truth, preserving the live-only convergence_reason. Children are refreshed
    (delete + reinsert) since a re-run may drop a section/iteration. Returns False when
    the run has no recordable row (incomplete / no audit trail)."""
    run_row = build_run_row(run_dir, summary=summary)
    if run_row is None:
        return False
    run_id = run_row["run_id"]
    conn.execute(_RUN_UPSERT, tuple(run_row[c] for c in RUN_COLUMNS))
    conn.execute("DELETE FROM run_sections WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM run_iterations WHERE run_id = ?", (run_id,))
    _insert(conn, "run_sections", SECTION_COLUMNS, build_section_rows(run_dir))
    _insert(conn, "run_iterations", ITERATION_COLUMNS, build_iteration_rows(run_dir))
    return True


def record_run_complete(run_id: str, *, output_dir: str | Path = "outputs",
                        summary: dict | None = None) -> bool:
    """Write a completed run's three tables in a single transaction (§3).

    Called at the ``run_complete`` event (``tailor/run.py``) for every run — CLI and
    API alike — alongside the existing on-disk checkpoints, which remain the source of
    truth. The UPSERT makes re-runs/retries idempotent. Reads the run's checkpoints from
    ``outputs/<run_id>/``; ``summary`` supplies the in-memory-only convergence_reason."""
    run_dir = Path(output_dir) / run_id
    with get_db(output_dir) as conn:
        return write_run(conn, run_dir, summary=summary)


# --------------------------------------------------------------------------- #
# Read path — paginated run list (API §4.2)                                    #
# --------------------------------------------------------------------------- #

# The subset of ``runs`` columns the list endpoint returns (§4.2 + the §5.2 owner-management
# fields: company_name, keep, cost_usd, unsupported_claims — so RunsPage needs no filesystem scan).
_LIST_FIELDS = (
    "run_id", "ts", "mode", "status", "fit_outcome", "fit_score", "coverage_score",
    "quality_score", "job_radar_job_id", "rerun_of", "public_demo", "jd_role_title",
    "company_name", "keep", "cost_usd", "unsupported_claims", "iterations_run",
)


def query_runs(output_dir: str | Path = "outputs", *, limit: int = 20, offset: int = 0,
               mode: str | None = None, public_only: bool = False) -> dict:
    """Paginated run list, newest first (§4.2). Returns ``{runs, total, limit, offset}``.

    Filters: ``mode`` ('demo'|'full'), ``public_only`` (only ``public_demo`` runs). The
    schema is created on demand, so a fresh deploy (no DB yet) returns an empty list
    rather than erroring."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, params = [], []
    if mode:
        where.append("mode = ?")
        params.append(mode)
    if public_only:
        where.append("public_demo = 1")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db(output_dir) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM runs{clause}", params).fetchone()[0]
        cols = ", ".join(_LIST_FIELDS)
        rows = conn.execute(
            f"SELECT {cols} FROM runs{clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    runs = []
    for r in rows:
        d = dict(r)
        d["public_demo"] = bool(d["public_demo"])
        d["keep"] = bool(d["keep"])
        runs.append(d)
    return {"runs": runs, "total": total, "limit": limit, "offset": offset}


def get_run_detail(run_id: str, output_dir: str | Path = "outputs") -> dict | None:
    """One run's structured detail from SQLite, shaped for the run-detail API (§4.1):
    scalars + fit + scores(iterations) + sections. Returns None if the run isn't in
    SQLite (a pre-migration run) — the endpoint then degrades to disk/nulls. The
    disk-only fields (`cv_final_md`, `jd_raw`, `fit.gaps`, grounding) are added by the
    caller; SQLite owns the structured/tabular data so new columns surface in the UI
    without regenerating anything (§5.1)."""
    with get_db(output_dir) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        sections = [dict(s) for s in conn.execute(
            "SELECT * FROM run_sections WHERE run_id = ? ORDER BY position, section_id",
            (run_id,)).fetchall()]
        iterations = [dict(it) for it in conn.execute(
            "SELECT * FROM run_iterations WHERE run_id = ? ORDER BY iteration",
            (run_id,)).fetchall()]

    for s in sections:                       # ints → bools for the client
        s["static"] = bool(s["static"])
        s["converged"] = None if s["converged"] is None else bool(s["converged"])
    return {
        "run_id": run["run_id"],
        "ts": run["ts"],
        "mode": run["mode"],
        "status": run["status"],
        "job_radar_job_id": run["job_radar_job_id"],
        "rerun_of": run["rerun_of"],
        "public_demo": bool(run["public_demo"]),
        "cost_usd": run["cost_usd"],
        "cvcm_enabled": bool(run["cvcm_enabled"]),
        "convergence_reason": run["convergence_reason"],
        "fit": {
            "outcome": run["fit_outcome"],
            "score": run["fit_score"],
            "role_title": run["jd_role_title"],
            "value_alignment": run["value_alignment"],
            "no_fit_reason": run["no_fit_reason"],
            "gaps": [],                      # filled from disk by the caller (not in SQLite)
        },
        "scores": {
            "coverage": run["coverage_score"],
            "quality": run["quality_score"],
            "iterations": iterations,
        },
        "sections": sections,
    }
