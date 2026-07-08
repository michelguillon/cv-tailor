"""api/archive.py — filesystem helpers for run output dirs (path safety, downloads, retention).

A run's durable record is its output directory (checkpoints + run_log.jsonl + cv_final.md),
written identically by CLI and UI runs. Since Phase 3 the run **list** and **detail** are served
from SQLite (`tailor/db.py`) via `GET /api/runs` and `GET /api/runs/{id}`, so the old
filesystem-scan `list_runs`/`run_detail` were retired. What remains here is the on-disk plumbing the
API still needs: path-safe run-dir resolution, artifact downloads, deletion, and retention cleanup.
The visibility flags (`public_demo`/`keep`) now live in SQLite, so `is_public`/`cleanup_runs` read
them via `db.get_run_creation_meta` (which falls back to the `run_meta.json` sidecar for a
pre-Phase-3 run).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.run_meta import created_at_from_id
from tailor import db

__all__ = ["run_file", "delete_run", "cleanup_runs",
           "is_public", "run_dir_if_exists", "retention_days_env"]

DOWNLOADABLE = {"cv_final.md", "cv_final.html"}


def _run_dir(output_dir: str | Path, run_id: str) -> Path | None:
    """Resolve outputs/<run_id>/, refusing path traversal (run_id from a URL)."""
    base = Path(output_dir).resolve()
    target = (base / run_id).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def run_dir_if_exists(output_dir: str | Path, run_id: str) -> Path | None:
    """Path-safe resolve of an *existing* run dir (a run_id from a URL), or None."""
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None or not (run_dir / "run_log.jsonl").exists():
        return None
    return run_dir


def is_public(output_dir: str | Path, run_id: str) -> bool:
    """Whether a run is marked public demo (so it's viewable without an unlock). Since Phase 3 the
    `public_demo` flag lives in SQLite (`PATCH` writes it there); read via `db.get_run_creation_meta`
    (sidecar fallback for a pre-Phase-3 run). A hot path (called per `_viewable` check)."""
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None or not (run_dir / "run_log.jsonl").exists():
        return False
    return bool(db.get_run_creation_meta(run_id, output_dir)["public_demo"])


def run_file(output_dir: str | Path, run_id: str, name: str) -> Path | None:
    """Path to a downloadable artifact (cv_final.md/.html), or None."""
    if name not in DOWNLOADABLE:
        return None
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None:
        return None
    path = run_dir / name
    return path if path.exists() else None


def delete_run(output_dir: str | Path, run_id: str) -> bool:
    """Remove a run's output directory entirely. Returns False if it doesn't exist or the
    run_id is unsafe (never deletes the base outputs/ dir itself). Owner-only — §12.9."""
    base = Path(output_dir).resolve()
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None or run_dir == base or not run_dir.is_dir():
        return False
    shutil.rmtree(run_dir, ignore_errors=True)
    return True


def retention_days_env() -> float | None:
    """The `RUN_RETENTION_DAYS` retention window, or None when unset/invalid/≤0 — in which
    case automatic cleanup is OFF (so dev/test never delete real runs, §12.9 / D-40)."""
    raw = os.environ.get("RUN_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = float(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def cleanup_runs(output_dir: str | Path, max_age_days: float,
                 *, now: datetime | None = None) -> list[str]:
    """Delete runs older than `max_age_days` unless `keep` or `public_demo` (D-40 retention).

    Age comes from the run id's timestamp (`run_YYYYMMDD_HHMMSS`), never a file mtime, so a
    later sidecar write can't reset the clock; an unparseable id is left alone (conservative).
    Returns the removed run ids."""
    base = Path(output_dir)
    if not base.is_dir():
        return []
    now = datetime.now(timezone.utc) if now is None else now
    cutoff = now - timedelta(days=max_age_days)
    removed = []
    for d in base.iterdir():
        if not (d.is_dir() and (d / "run_log.jsonl").exists()):
            continue
        meta = db.get_run_creation_meta(d.name, output_dir)   # keep/public_demo in SQLite since Phase 3
        if meta["keep"] or meta["public_demo"]:
            continue
        created = created_at_from_id(d.name)
        if created is None or datetime.fromisoformat(created) >= cutoff:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed
