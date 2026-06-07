"""api/archive.py — read completed runs from outputs/<run_id>/ for replay/showcase.

A run's durable record is its output directory (checkpoints + run_log.jsonl +
cv_final.*), written identically by CLI and UI runs. This reads them back — the same
data the CLI `replay` command surfaces — so the UI can browse and re-view any past
run (including the preserved no-spend demo runs) without re-spending. Read-only.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.run_meta import created_at_from_id, read_meta
from tailor.audit import read_entries
from tailor.phases.phase6_output import summary_card

__all__ = ["list_runs", "run_detail", "run_file", "delete_run", "cleanup_runs",
           "is_public", "run_dir_if_exists", "retention_days_env"]

DOWNLOADABLE = {"cv_final.md", "cv_final.html"}

# Owner-only fields blanked from the redacted public view (§12.9 — public ≠ full metadata).
_REDACTED = ("cost_estimated_usd", "cost_breakdown", "created_at", "unsupported_claims")


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
    # Summary card (D-34): derive from the footer's grounded_coverage + fabrication_flags
    # via the same helper Phase 6 uses (single source of truth, F-43). Old runs whose
    # footer predates these fields → card numbers are None (the UI degrades gracefully).
    grounded = footer.get("grounded_coverage")
    unsupported = footer.get("fabrication_flags")
    card = summary_card(outcome or "", fit_score, grounded, unsupported or 0)
    meta = read_meta(run_dir)                       # visibility/retention sidecar (D-40)
    return {
        "run_id": run_dir.name,
        "created_at": created_at_from_id(run_dir.name),
        "company_name": meta["company_name"],
        "mode": footer.get("mode"),
        "role_title": role_title,
        "outcome": outcome,
        "fit_score": fit_score,
        "iterations": footer.get("iterations_run"),
        "cost_estimated_usd": footer.get("total_estimated_usd"),
        "cost_breakdown": footer.get("cost_breakdown_estimated_usd"),
        "grounded_coverage": grounded,
        "unsupported_claims": unsupported,
        "status": card["status"] if outcome is not None else None,
        "fit_band": card["fit_band"] if fit_score is not None else None,
        "keep": meta["keep"],
        "public_demo": meta["public_demo"],
        "has_md": (run_dir / "cv_final.md").exists(),
        "has_html": (run_dir / "cv_final.html").exists(),
    }


def _redact(summary: dict) -> dict:
    """Blank owner-only fields for the public view (curated demo runs, §12.9)."""
    return {**summary, **{k: None for k in _REDACTED}}


def is_public(output_dir: str | Path, run_id: str) -> bool:
    """Whether a run is marked public demo (so it's viewable without an unlock)."""
    run_dir = _run_dir(output_dir, run_id)
    if run_dir is None or not (run_dir / "run_log.jsonl").exists():
        return False
    return read_meta(run_dir)["public_demo"]


def list_runs(output_dir: str | Path = "outputs", *, include_private: bool = True,
              redact: bool = False) -> list[dict]:
    """Run summaries, newest first (by directory name = timestamped id).

    `include_private=False` returns only `public_demo` runs (the public view); `redact=True`
    blanks owner-only fields (cost, created_at, grounding internals) — §12.9 / D-40."""
    base = Path(output_dir)
    if not base.is_dir():
        return []
    dirs = sorted([d for d in base.iterdir() if d.is_dir() and (d / "run_log.jsonl").exists()],
                  key=lambda d: d.name, reverse=True)
    out = []
    for d in dirs:
        s = _summary(d)
        if not include_private and not s["public_demo"]:
            continue
        out.append(_redact(s) if redact else s)
    return out


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
        meta = read_meta(d)
        if meta["keep"] or meta["public_demo"]:
            continue
        created = created_at_from_id(d.name)
        if created is None or datetime.fromisoformat(created) >= cutoff:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed
