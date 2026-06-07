"""api/run_meta.py — mutable per-run visibility/retention sidecar (SPEC §12.9, D-40).

A run's durable record (`run_log.jsonl`, phase checkpoints, cv_final.*) is append-only and
immutable (audit ≠ context, D-06). Visibility (`public_demo`), retention (`keep`), and an
editable `company_name` are *mutable* owner state, so they live in a separate sidecar
`outputs/<run_id>/run_meta.json` — orthogonal to the model/cost `mode`.

The sidecar is absent on every pre-existing run, so reads default to **private, not kept**
(`public_demo=false, keep=false`) — old runs need no migration. `created_at` is the run id's
UTC timestamp (`run_YYYYMMDD_HHMMSS`); cleanup ages runs by that id, never by a file mtime
(a sidecar write would otherwise reset the clock).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["META_FILE", "default_meta", "read_meta", "write_meta", "created_at_from_id"]

META_FILE = "run_meta.json"

# Only these keys are persisted/editable; anything else in a PATCH body is ignored.
_FIELDS = ("company_name", "keep", "public_demo")


def default_meta() -> dict:
    """The meta of a run with no sidecar yet: private, not kept, no company."""
    return {"company_name": None, "keep": False, "public_demo": False}


def created_at_from_id(run_id: str) -> str | None:
    """Parse the UTC timestamp embedded in a `run_YYYYMMDD_HHMMSS` id → ISO 8601, or None."""
    try:
        dt = datetime.strptime(run_id, "run_%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


def read_meta(run_dir: Path) -> dict:
    """Read a run's sidecar, falling back to defaults for absent file/keys (fail-safe)."""
    meta = default_meta()
    path = run_dir / META_FILE
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                meta.update({k: stored[k] for k in _FIELDS if k in stored})
        except (json.JSONDecodeError, OSError):
            pass  # a corrupt sidecar must not break listing — treat as defaults
    return meta


def write_meta(run_dir: Path, **fields) -> dict:
    """Merge the given fields into the sidecar and persist it; returns the full meta.

    Only the known mutable fields are written; the run dir must already exist."""
    meta = read_meta(run_dir)
    for k in _FIELDS:
        if k in fields and fields[k] is not None:
            meta[k] = fields[k]
    (run_dir / META_FILE).write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    return meta
