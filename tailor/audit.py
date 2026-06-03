"""Audit trail logger → run_log.jsonl (SPEC_ORCHESTRATOR.md §3.6, D-06).

A pure, typed, append-only logger. No API calls — needed from Phase 2 onward.
Orchestrator reasoning is written here but never injected back into the messages
array: context stays clean, the audit trail stays complete and inspectable. The
HTML output (Phase 6) renders these entries as a collapsible reasoning trace,
and `python -m tailor replay <run_id>` reads them back.

One JSON object per line, same format as the Week 2 session transcripts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import ReasoningEntry

__all__ = ["utc_now_iso", "AuditLogger", "read_entries"]


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string, e.g. '2026-06-03T14:23:01Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLogger:
    """Append-only writer for ``run_log.jsonl``.

    Each :meth:`log` call serialises one ``ReasoningEntry`` as a JSON line and
    flushes immediately, so a crash mid-run leaves a complete trail up to the
    last logged decision (the checkpoint principle applied to observability).
    """

    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- typed reasoning entries -------------------------------------------- #

    def log(self, entry: ReasoningEntry) -> ReasoningEntry:
        """Append a fully-constructed ``ReasoningEntry``."""
        self._write(entry.to_dict())
        return entry

    def log_event(
        self,
        phase: str,
        event: str,
        reasoning: str,
        *,
        iteration: int | None = None,
        keyword_score: float | None = None,
        critique_score: float | None = None,
        rubric_version: int | None = None,
        ts: str | None = None,
    ) -> ReasoningEntry:
        """Construct and append a ``ReasoningEntry`` in one call.

        ``ts`` defaults to the current UTC time; pass it explicitly only to make
        tests deterministic.
        """
        entry = ReasoningEntry(
            ts=ts or utc_now_iso(),
            phase=phase,
            event=event,
            reasoning=reasoning,
            iteration=iteration,
            keyword_score=keyword_score,
            critique_score=critique_score,
            rubric_version=rubric_version,
        )
        return self.log(entry)

    # -- run footer --------------------------------------------------------- #

    def log_footer(self, footer: dict) -> dict:
        """Append a non-reasoning record (e.g. the run_complete cost footer, §9).

        Kept distinct from :meth:`log` because the footer is not a
        ``ReasoningEntry`` — it is a different record shape on the same stream.
        """
        self._write(footer)
        return footer

    # -- internals ---------------------------------------------------------- #

    def _write(self, record: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")


def read_entries(log_path: str | Path) -> list[dict]:
    """Read a ``run_log.jsonl`` file back into a list of dicts (for replay).

    Blank lines are skipped. Returns raw dicts rather than typed objects because
    the stream is heterogeneous (reasoning entries + the run footer); callers
    that want a ``ReasoningEntry`` can pass a record to ``ReasoningEntry.from_dict``.
    """
    path = Path(log_path)
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
