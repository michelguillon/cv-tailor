"""tailor/run_context.py — per-run output dir + section checkpoints + audit log.

Realises the checkpoint pattern (SPEC §8, R-06): every phase writes its output
under outputs/<run_id>/ before the next phase starts, so a failed run is
inspectable up to that point. Section drafts are versioned files on disk, never
fields on PipelineOutput (D-07 #3); Phase 6 assembles from these files.

Layout:
    outputs/<run_id>/
      sections/<section_id>_v<n>.md   (drafted, versioned)
      sections/<section_id>_static.md (copied verbatim)
      run_log.jsonl                   (audit trail)
      phase*.json                     (phase checkpoints)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tailor.audit import AuditLogger

__all__ = ["RunContext", "new_run_id"]


def new_run_id() -> str:
    """Timestamped run id, e.g. 'run_20260604_142301'."""
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


@dataclass
class RunContext:
    run_id: str
    output_dir: Path

    @classmethod
    def create(cls, *, run_id: str | None = None, base_dir: str | Path = "outputs") -> "RunContext":
        rid = run_id or new_run_id()
        out = Path(base_dir) / rid
        (out / "sections").mkdir(parents=True, exist_ok=True)
        return cls(run_id=rid, output_dir=out)

    # -- sections ----------------------------------------------------------- #

    @property
    def sections_dir(self) -> Path:
        return self.output_dir / "sections"

    def section_path(self, section_id: str, *, version: int | None = None, static: bool = False) -> Path:
        if static:
            name = f"{section_id}_static.md"
        else:
            if version is None:
                raise ValueError("version is required for non-static section files")
            name = f"{section_id}_v{version}.md"
        return self.sections_dir / name

    def write_section(self, section_id: str, text: str, *, version: int | None = None, static: bool = False) -> Path:
        path = self.section_path(section_id, version=version, static=static)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def read_section(self, section_id: str, *, version: int | None = None, static: bool = False) -> str:
        return self.section_path(section_id, version=version, static=static).read_text(encoding="utf-8")

    # -- phase checkpoints + audit ----------------------------------------- #

    def write_checkpoint(self, name: str, obj) -> Path:
        """Persist a Serializable phase output (or plain dict) as JSON."""
        data = obj.to_dict() if hasattr(obj, "to_dict") else obj
        path = self.output_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @property
    def audit(self) -> AuditLogger:
        # One logger per run; AuditLogger appends, so re-creating is fine.
        return AuditLogger(self.output_dir / "run_log.jsonl")
