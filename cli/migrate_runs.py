"""cli/migrate_runs.py — backfill the SQLite run store from existing runs.

One-time (idempotent) migration per SPEC_SQLITE_MIGRATION §3: read every
``outputs/<run_id>/`` (its ``run_log.jsonl`` footer, phase checkpoints, iteration
scores, draft manifest, and the ``run_meta.json`` sidecar) and populate
``data/cv_tailor.db``. Existing runs that predate a field get NULL for it (§7).

Idempotent: rows are written with an UPSERT that syncs every disk-derived column from the
on-disk checkpoints while preserving the live-only ``convergence_reason`` (a migration passes
NULL for it, so an existing row keeps its value). Re-running is therefore also a **repair/sync**
— it backfills columns added after a run was first recorded (§5.2). Runs with no recordable
footer (crashed before completion) are skipped, not errored.

The row mapping is the **same** code the live write path uses (``tailor/db.write_run``)
— there is no second disk→row mapping to drift (F-59). Run it against real data
before trusting the live write path; it validates the schema against every existing
run shape (old and new).

Usage (inside the cli container)::

    docker compose run --rm cli python -m cli.migrate_runs            # outputs/ → data/cv_tailor.db
    docker compose run --rm cli python -m cli.migrate_runs --dry-run  # report only, no writes
    docker compose run --rm cli python -m cli.migrate_runs --output-dir outputs --db data/cv_tailor.db
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tailor import db


def _run_dirs(output_dir: Path) -> list[Path]:
    """Completed-or-failed run dirs (those with an audit trail), sorted by id."""
    if not output_dir.is_dir():
        return []
    return sorted((d for d in output_dir.iterdir()
                   if d.is_dir() and (d / "run_log.jsonl").exists()),
                  key=lambda d: d.name)


def migrate(output_dir: str | Path = "outputs", *, dry_run: bool = False) -> dict:
    """Backfill SQLite from ``output_dir``. Returns ``{recorded, skipped, total}``.

    ``recorded`` counts run dirs that produced a row this pass (already-present rows
    still count as recorded — the operation is declarative, INSERT OR IGNORE). ``skipped``
    counts dirs with no recordable footer."""
    output_dir = Path(output_dir)
    dirs = _run_dirs(output_dir)
    recorded = skipped = 0
    skipped_ids: list[str] = []
    if dry_run:
        for d in dirs:
            row = db.build_run_row(d)
            if row is None:
                skipped += 1
                skipped_ids.append(d.name)
            else:
                recorded += 1
                print(f"  would record {d.name}  "
                      f"[{row['status']}, mode={row['mode']}, outcome={row['fit_outcome']}]")
    else:
        with db.get_db(output_dir) as conn:
            for d in dirs:
                if db.write_run(conn, d):        # idempotent UPSERT — syncs disk, keeps live-only fields
                    recorded += 1
                else:
                    skipped += 1
                    skipped_ids.append(d.name)
    return {"recorded": recorded, "skipped": skipped, "total": len(dirs),
            "skipped_ids": skipped_ids, "db": str(db.db_path_for(output_dir))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the SQLite run store from outputs/.")
    parser.add_argument("--output-dir", default="outputs",
                        help="runs directory to read (default: outputs)")
    parser.add_argument("--db", default=None,
                        help="SQLite path (default: <output-dir>/../data/cv_tailor.db, "
                             "or $CV_TAILOR_DB)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be recorded; write nothing")
    args = parser.parse_args(argv)

    if args.db:
        os.environ["CV_TAILOR_DB"] = args.db        # db.db_path_for honours this override

    result = migrate(args.output_dir, dry_run=args.dry_run)
    verb = "Would record" if args.dry_run else "Recorded"
    print(f"\n{verb} {result['recorded']} run(s); skipped {result['skipped']} "
          f"(no completion footer) of {result['total']} dir(s).")
    if result["skipped_ids"]:
        print("  skipped: " + ", ".join(result["skipped_ids"]))
    if not args.dry_run:
        print(f"  database: {result['db']}")
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
