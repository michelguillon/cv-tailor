"""cli/reconcile_runs.py — the soak gate for the SQLite migration.

The migration ships each phase as a **dual-write / coexistence** step (the old stores stay
authoritative; SQLite shadows them), soaks ~1 week in prod, then advances **only on a clean
reconciliation** — hardest before Phase 3, the one irreversible step that retires the old
write paths (SPEC_SQLITE_MIGRATION §6, "Migration methodology").

This script is that gate. It compares the SQLite store against the filesystem truth, run by
run, and exits non-zero on any *real* divergence so it can gate a deploy. For each run dir it
**rebuilds the expected row from the current on-disk checkpoints** with the same canonical
builder the write path uses (``tailor/db.build_run_row``) and diffs it against the stored row —
so a row builder bug, a missed run, or an orphaned row all surface.

Two field classes are *not* hard failures (reported separately, never gate):
- ``public_demo`` / ``keep`` — mutable visibility flags. In Phase 1/2, ``PATCH /{id}/meta``
  writes only the sidecar, so SQLite's copy goes stale after a post-completion toggle (known;
  Phase 3 makes PATCH write SQLite). Reported as **drift (expected)**.
- ``convergence_reason`` — persisted to no checkpoint (it rides the in-memory summary on the
  live path; NULL for migrated runs). The disk rebuild can't reproduce it, so it's **skipped**.

Usage (inside the cli container)::

    docker compose run --rm cli python -m cli.reconcile_runs            # gate: exit 0 = clean
    docker compose run --rm cli python -m cli.reconcile_runs --verbose  # list every run checked
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tailor import db

# Fields whose disk-vs-SQLite difference is expected, not a bug (see module docstring).
# public_demo/keep/company_name are mutable sidecar state (PATCH writes the sidecar, not SQLite,
# until Phase 3), so SQLite can lag until the next write/migration.
DRIFT_OK = ("public_demo", "keep", "company_name")
SKIP = ("convergence_reason",)         # DB-only; no checkpoint to rebuild from


def _run_dirs(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return sorted((d for d in output_dir.iterdir()
                   if d.is_dir() and (d / "run_log.jsonl").exists()),
                  key=lambda d: d.name)


def reconcile(output_dir: str | Path = "outputs") -> dict:
    """Compare SQLite against the filesystem. Returns a report dict; ``clean`` is the gate."""
    output_dir = Path(output_dir)
    # Expected rows: rebuilt from current disk (None = a run with no recordable footer → skip).
    expected: dict[str, dict] = {}
    section_counts: dict[str, int] = {}
    iter_counts: dict[str, int] = {}
    for d in _run_dirs(output_dir):
        row = db.build_run_row(d)
        if row is None:
            continue
        expected[d.name] = row
        section_counts[d.name] = len(db.build_section_rows(d))
        iter_counts[d.name] = len(db.build_iteration_rows(d))

    with db.get_db(output_dir) as conn:
        stored = {r["run_id"]: dict(r)
                  for r in conn.execute("SELECT * FROM runs").fetchall()}
        sec_have = {row[0]: row[1] for row in conn.execute(
            "SELECT run_id, COUNT(*) FROM run_sections GROUP BY run_id").fetchall()}
        iter_have = {row[0]: row[1] for row in conn.execute(
            "SELECT run_id, COUNT(*) FROM run_iterations GROUP BY run_id").fetchall()}

    missing = sorted(set(expected) - set(stored))        # on disk, recordable, not in SQLite
    orphan = sorted(set(stored) - set(expected))         # in SQLite, no recordable disk run
    mismatches: list[dict] = []                          # real field divergences (gate)
    drift: list[dict] = []                               # expected sidecar staleness (info)

    for run_id in sorted(set(expected) & set(stored)):
        exp, got = expected[run_id], stored[run_id]
        for col in db.RUN_COLUMNS:
            if col in SKIP:
                continue
            if exp.get(col) != got.get(col):
                rec = {"run_id": run_id, "field": col,
                       "disk": exp.get(col), "sqlite": got.get(col)}
                (drift if col in DRIFT_OK else mismatches).append(rec)
        if section_counts.get(run_id, 0) != sec_have.get(run_id, 0):
            mismatches.append({"run_id": run_id, "field": "section_count",
                               "disk": section_counts.get(run_id, 0),
                               "sqlite": sec_have.get(run_id, 0)})
        if iter_counts.get(run_id, 0) != iter_have.get(run_id, 0):
            mismatches.append({"run_id": run_id, "field": "iteration_count",
                               "disk": iter_counts.get(run_id, 0),
                               "sqlite": iter_have.get(run_id, 0)})

    return {
        "disk_runs": len(expected), "sqlite_runs": len(stored),
        "missing": missing, "orphan": orphan,
        "mismatches": mismatches, "drift": drift,
        "checked": sorted(set(expected) & set(stored)),
        "clean": not (missing or orphan or mismatches),
        "db": str(db.db_path_for(output_dir)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile the SQLite run store against the filesystem (migration soak gate).")
    parser.add_argument("--output-dir", default="outputs", help="runs directory (default: outputs)")
    parser.add_argument("--verbose", action="store_true", help="list every run checked")
    args = parser.parse_args(argv)

    r = reconcile(args.output_dir)
    print(f"database: {r['db']}")
    print(f"runs: {r['disk_runs']} on disk (recordable) | {r['sqlite_runs']} in SQLite | "
          f"{len(r['checked'])} compared\n")

    if r["missing"]:
        print(f"[FAIL] {len(r['missing'])} run(s) on disk but NOT in SQLite (not yet recorded — "
              f"run the migration):")
        for rid in r["missing"]:
            print(f"    {rid}")
    if r["orphan"]:
        print(f"[FAIL] {len(r['orphan'])} run(s) in SQLite with no recordable disk run (orphan):")
        for rid in r["orphan"]:
            print(f"    {rid}")
    if r["mismatches"]:
        print(f"[FAIL] {len(r['mismatches'])} field mismatch(es) — SQLite disagrees with the checkpoints:")
        for m in r["mismatches"]:
            print(f"    {m['run_id']}  {m['field']}: disk={m['disk']!r}  sqlite={m['sqlite']!r}")
    if r["drift"]:
        print(f"[info] {len(r['drift'])} expected drift (mutable flag PATCHed after the SQLite "
              f"write — Phase 3 propagates PATCH -> SQLite):")
        for d in r["drift"]:
            print(f"    {d['run_id']}  {d['field']}: disk={d['disk']!r}  sqlite={d['sqlite']!r}")
    if args.verbose:
        print("\nchecked:")
        for rid in r["checked"]:
            print(f"    {rid}")

    if r["clean"]:
        print("\n[OK] CLEAN — SQLite matches the filesystem. Safe to advance to the next phase.")
        return 0
    print("\n[FAIL] NOT CLEAN — resolve the above before advancing. SQLite is additive, so the "
          "old paths are unaffected meanwhile.")
    return 1


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
