"""api/archive.py — read completed runs from outputs/<run_id>/ for replay/showcase.

A run's durable record is its output directory (checkpoints + run_log.jsonl +
cv_final.*), written identically by CLI and UI runs. This reads them back — the same
data the CLI `replay` command surfaces — so the UI can browse and re-view any past
run (including the preserved no-spend demo runs) without re-spending. Read-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from tailor.audit import read_entries

__all__ = ["list_runs", "run_detail", "run_file"]

DOWNLOADABLE = {"cv_final.md", "cv_final.html"}


def _run_dir(output_dir: str | Path, run_id: str) -> Path | None:
    """Resolve outputs/<run_id>/, refusing path traversal (run_id from a URL)."""
    base = Path(output_dir).resolve()
    target = (base / run_id).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def _footer(run_dir: Path) -> dict:
    for entry in reversed(read_entries(run_dir / "run_log.jsonl")):
        if entry.get("type") == "run_complete":
            return entry
    return {}


def _summary(run_dir: Path) -> dict:
    footer = _footer(run_dir)
    role_title = outcome = fit_score = None
    p0 = run_dir / "phase0_jd_analysis.json"
    if p0.exists():
        role_title = json.loads(p0.read_text(encoding="utf-8")).get("role_title")
    p1 = run_dir / "phase1_fit_assessment.json"
    if p1.exists():
        fit = json.loads(p1.read_text(encoding="utf-8"))
        outcome = fit.get("outcome")
        fit_score = fit.get("overall_fit_score")
    return {
        "run_id": run_dir.name,
        "mode": footer.get("mode"),
        "role_title": role_title,
        "outcome": outcome,
        "fit_score": fit_score,
        "iterations": footer.get("iterations_run"),
        "cost_estimated_usd": footer.get("total_estimated_usd"),
        "cost_breakdown": footer.get("cost_breakdown_estimated_usd"),
        "has_md": (run_dir / "cv_final.md").exists(),
        "has_html": (run_dir / "cv_final.html").exists(),
    }


def list_runs(output_dir: str | Path = "outputs") -> list[dict]:
    """Every run dir with a run_log, newest first (by directory name = timestamped id)."""
    base = Path(output_dir)
    if not base.is_dir():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir() and (d / "run_log.jsonl").exists()]
    return [_summary(d) for d in sorted(dirs, key=lambda d: d.name, reverse=True)]


def run_detail(output_dir: str | Path, run_id: str) -> dict | None:
    """Full replay payload: summary + per-iteration scores + the reasoning trace."""
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None or not (run_dir / "run_log.jsonl").exists():
        return None
    detail = _summary(run_dir)
    iters = sorted(run_dir.glob("iteration_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    detail["iteration_scores"] = [json.loads(p.read_text(encoding="utf-8")) for p in iters]
    detail["reasoning"] = [
        e for e in read_entries(run_dir / "run_log.jsonl") if e.get("type") != "run_complete"
    ]
    md = run_dir / "cv_final.md"
    detail["cv_md"] = md.read_text(encoding="utf-8") if md.exists() else None
    return detail


def run_file(output_dir: str | Path, run_id: str, name: str) -> Path | None:
    """Path to a downloadable artifact (cv_final.md/.html), or None."""
    if name not in DOWNLOADABLE:
        return None
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None:
        return None
    path = run_dir / name
    return path if path.exists() else None
