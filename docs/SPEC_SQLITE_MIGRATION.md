# SPEC_SQLITE_MIGRATION.md
## cv-tailor — SQLite Run Store + Dynamic Report

**Status:** Phases 1–3 built (F-59/F-60) — the migration is complete  
**Scope:** SQLite run store · API-driven run detail page · dynamic report tabs  
**Effort:** ~2–3 days  
**Motivation:** Static `cv_final.html` cannot accommodate new fields without
regeneration. Run metadata is scattered across `run_meta.json`, `run_log.jsonl`,
and section files — not queryable. Architecture converges with Job Radar
(SQLite + JSONL + filesystem), enabling Phase 4 cross-system analysis.

---

## 1. Architecture after migration

```
outputs/<run_id>/
  sections/              ← section files, per-writer drafts (unchanged)
  run_log.jsonl          ← append-only audit trail (unchanged)
  jd_raw.txt             ← raw JD (unchanged)
  cv_final.md            ← clean CV download artifact (unchanged)
  cv_final.html          ← DEPRECATED: generated on-demand only, not at run time

data/
  cv_tailor.db           ← SQLite: runs, sections, iterations (NEW)
  chroma/                ← unchanged
  cvs/                   ← unchanged

GET /api/runs/{id}       ← structured JSON, drives all six report tabs (NEW)
GET /api/runs/{id}/html  ← generates cv_final.html on demand (replaces static)
```

**Invariant:** SQLite holds structured metadata. JSONL holds the full audit
trail. Filesystem holds binary artifacts and section text. They are
complementary, not redundant.

---

## 2. Schema

```sql
-- One row per run
CREATE TABLE runs (
    run_id              TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,              -- ISO8601
    mode                TEXT NOT NULL,              -- 'demo' | 'full'
    status              TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'complete' | 'failed'
    job_radar_job_id    TEXT,                       -- null if not from Job Radar
    rerun_of            TEXT,                       -- null if original run
    public_demo         INTEGER NOT NULL DEFAULT 0, -- boolean
    keep                INTEGER NOT NULL DEFAULT 0, -- boolean, protects from cleanup
    fit_score           REAL,
    fit_outcome         TEXT,                       -- 'strong' | 'partial' | 'no_fit'
    coverage_score      REAL,
    quality_score       REAL,
    cvcm_enabled        INTEGER NOT NULL DEFAULT 0,
    convergence_reason  TEXT,
    iterations_run      INTEGER,
    cost_usd            REAL,
    jd_role_title       TEXT,                       -- extracted from JD for display
    value_alignment     TEXT,                       -- CVCM alignment notes
    no_fit_reason       TEXT                        -- populated when fit_outcome='no_fit'
);

-- One row per non-static section per run
CREATE TABLE run_sections (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    section_id      TEXT NOT NULL,
    section_type    TEXT NOT NULL,
    position        INTEGER NOT NULL,
    static          INTEGER NOT NULL DEFAULT 0,
    final_version   INTEGER,
    converged       INTEGER,
    keyword_coverage REAL,
    claude_quality  REAL,
    gpt_quality     REAL,
    selected_writer TEXT,                           -- 'claude' | 'gpt' | 'synthesis'
    source_cv       TEXT,                           -- which CV this section came from
    PRIMARY KEY (run_id, section_id)
);

-- One row per iteration per run
CREATE TABLE run_iterations (
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

-- Indexes for common queries
CREATE INDEX idx_runs_ts ON runs(ts DESC);
CREATE INDEX idx_runs_job_radar ON runs(job_radar_job_id) WHERE job_radar_job_id IS NOT NULL;
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_public ON runs(public_demo) WHERE public_demo = 1;
```

---

## 3. Write path

SQLite is written at run completion, not incrementally during the run.
The pipeline writes to disk (section files, JSONL) as before. At the
`run_complete` event, a single transaction writes all three tables.

```python
# tailor/db.py
def record_run_complete(run_id: str, pipeline_output: PipelineOutput) -> None:
    """Write run, sections, and iterations to SQLite in a single transaction."""
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO runs VALUES (...)", run_row)
        conn.executemany("INSERT INTO run_sections VALUES (...)", section_rows)
        conn.executemany("INSERT INTO run_iterations VALUES (...)", iteration_rows)
        conn.commit()
```

`INSERT OR REPLACE` on `runs` means re-runs and retries are idempotent.
If the pipeline fails before `run_complete`, no SQLite record is written —
the run is only in SQLite if it completed successfully.

**Database location:** `data/cv_tailor.db` (bind-mounted, backed up with
other data/ contents).

**Migration of existing runs:** a one-time `cli/migrate_runs.py` script
reads all `outputs/<run_id>/run_meta.json` + `run_log.jsonl` files and
populates SQLite. Existing runs without all fields get nulls for new
columns. Script is idempotent (INSERT OR IGNORE).

**Creation-time metadata (Phase 3).** Some metadata is fixed at run *creation* and cannot be
rebuilt from the disk checkpoints (company label, `rerun_of`, Job Radar source/assessment/extraction,
visibility flags). Since Phase 3 the API writes it to a partial `status='running'` row at creation
(`db.record_run_start`); the completion write (`record_run_complete`) UPSERTs the disk-derived columns
**over** it while preserving the creation-owned ones via `COALESCE` (see Phase 3 in §6). This replaces
the retired `run_meta.json` sidecar write.

---

## 4. API

### 4.1 Run detail endpoint (replaces static HTML serving)

```
GET /api/runs/{run_id}
```

Returns structured JSON driving all six tabs. Owner-gated for private runs;
public for `public_demo=true` runs.

```json
{
  "run_id": "run_20260614_143000",
  "ts": "2026-06-14T14:30:00Z",
  "mode": "full",
  "status": "complete",
  "job_radar_job_id": "sha256:abc123",
  "rerun_of": "run_20260613_111401",
  "public_demo": false,
  "fit": {
    "outcome": "partial",
    "score": 0.74,
    "role_title": "Director of Solutions Engineering",
    "value_alignment": "...",
    "gaps": [...]
  },
  "scores": {
    "coverage": 0.83,
    "quality": 8.4,
    "iterations": [
      {"iteration": 1, "keyword_coverage": 0.74, "quality_score": 7.1, ...},
      {"iteration": 2, "keyword_coverage": 0.83, "quality_score": 8.4, ...}
    ]
  },
  "sections": [
    {
      "section_id": "profile",
      "section_type": "profile",
      "converged": true,
      "final_version": 2,
      "keyword_coverage": 0.88,
      "selected_writer": "synthesis",
      "source_cv": "CV_AI_Leadership"
    }
  ],
  "cost_usd": 0.79,
  "cvcm_enabled": true,
  "convergence_reason": "both_signals_plateau",
  "jd_raw": "...",
  "cv_final_md": "..."   -- read from cv_final.md on disk
}
```

### 4.2 Run list endpoint

```
GET /api/runs
```

Query params: `?limit=20&offset=0&mode=full&public_only=true`

Returns paginated list from SQLite — no filesystem scanning needed.

```json
{
  "runs": [
    {
      "run_id": "run_20260614_143000",
      "ts": "...",
      "mode": "full",
      "fit_outcome": "partial",
      "fit_score": 0.74,
      "coverage_score": 0.83,
      "quality_score": 8.4,
      "job_radar_job_id": "sha256:abc123",
      "rerun_of": null,
      "public_demo": false,
      "jd_role_title": "Director of Solutions Engineering"
    }
  ],
  "total": 47,
  "limit": 20,
  "offset": 0
}
```

### 4.3 On-demand HTML generation

```
GET /api/runs/{run_id}/html
```

Generates `cv_final.html` from current data (not from a cached file).
Returns the HTML file as a download. Replaces the static file approach —
the HTML is always up to date with whatever fields exist in the DB at
generation time.

### 4.4 Run meta update (Phase 3 — writes SQLite)

```
PATCH /api/runs/{run_id}/meta
```

Updates `public_demo`, `keep`, `company_name` (owner-only). Since Phase 3 it writes **SQLite only**
(`db.update_run_meta`) — the `run_meta.json` sidecar write is retired. If the run has no row yet it is
backfilled from disk (`write_run`) then updated, so a PATCH is never lost.

---

## 5. Frontend changes

### 5.1 Run detail page

Replace static HTML iframe / file serve with a proper React page that
calls `GET /api/runs/{id}` and renders six tabs from the JSON response.

**Tab rendering:**
- **Fit tab** — from `response.fit` (outcome, score, role title, CVCM notes, gaps)
- **CV tab** — render `response.cv_final_md` as markdown
- **Changes tab** — read section diff files from disk via a new
  `GET /api/runs/{id}/sections/{section_id}/diff` endpoint
- **Scores tab** — from `response.scores.iterations` + `response.sections`
- **Reasoning tab** — stream from `run_log.jsonl` via
  `GET /api/runs/{id}/reasoning`
- **JD tab** — from `response.jd_raw`

**New fields automatically appear** in the UI for all runs once they exist
in the DB — no HTML regeneration needed.

### 5.2 Run list page

Replace filesystem-scan-based run listing with `GET /api/runs` (SQLite
query). Enables sorting by fit score, filtering by mode, searching by
role title. Renders immediately without scanning the outputs directory.

### 5.3 Sticky summary card

Populated from `response.fit` + `response.scores` — same data as before,
now from the API rather than baked into static HTML.

---

## 6. Migration plan

### Migration methodology — dual-write, soak, reconcile (how we advance phases)

Every phase ships as a **dual-write / coexistence** step: the old stores stay authoritative
and keep driving the app, while the new store shadows them. We then **soak ~1 week in prod**
and advance **only on a clean reconciliation** — never on a timer alone. (Same discipline used
for the Job Radar storage migration.)

1. **Ship additive.** The new path writes alongside the old; nothing yet *depends* on it
   (Phase 1: SQLite is written at completion but only a direct-HTTP `GET /api/runs` reads it —
   the UI is untouched).
2. **Soak ~1 week.** Real prod runs exercise the new write path while the old path is still the
   source of truth, so a bug is invisible to users and reversible (delete the DB).
3. **Reconcile before advancing** — `cli/reconcile_runs.py` is the gate. It rebuilds each run's
   expected row from the current on-disk checkpoints (the same `tailor/db.build_run_row` the
   write path uses) and diffs it against the stored row, flagging missing rows, orphans, and
   field mismatches (`exit 1` gates a deploy). Two field classes are reported but never gate:
   `public_demo`/`keep` (mutable sidecar — SQLite refreshes only at write time until Phase 3
   makes `PATCH` write SQLite) and `convergence_reason` (no checkpoint to rebuild from).
   ```bash
   docker compose run --rm cli python -m cli.reconcile_runs            # exit 0 = clean = advance
   docker compose run --rm cli python -m cli.reconcile_runs --verbose
   ```
4. **Advance on green.** A clean reconcile after the soak is the go signal. The gate matters
   **most before Phase 3** — the one irreversible step that retires the old write paths.

### Phase 1 — Add SQLite alongside existing approach (1 day)

1. Add `tailor/db.py` — schema creation, `record_run_complete()`
2. Call `record_run_complete()` at `run_complete` event (alongside existing
   `run_meta.json` write — both coexist)
3. Add `cli/migrate_runs.py` — populate SQLite from existing run files
4. Run migration against all existing runs
5. Add `GET /api/runs` list endpoint (SQLite-backed)

*Nothing breaks. Existing UI unchanged. SQLite is additive.*

### Phase 2 — API-driven run detail (built)

1. `GET /api/runs/{run_id}` structured JSON (§4.1) — SQLite scalars/fit/scores/sections +
   disk `cv_final_md`/`jd_raw`/`gaps`/grounding + display fields (company, card, downloads,
   owner-gated Job Radar). Live status moved to `GET /{run_id}/status`.
2. `GET /api/runs/{run_id}/html` on-demand HTML (regenerated from checkpoints; promotes the
   F-40 regen helper to a supported path) + `/sections/{sid}/diff` + `/reasoning`.
3. Run detail page = six native React tabs from the API (no iframe).
4. Run list moved onto `GET /api/runs` (§5.2), capability-aware. This added two `runs`
   columns — **company_name** + **unsupported_claims** — so the owner list needs no disk read.

**Schema evolution (Phase 2 introduced the first post-deploy columns).** New columns are declared
in `SCHEMA` *and* listed in `_ADDED_COLUMNS`; `init_schema` `ALTER TABLE ADD COLUMN`s any that a
pre-existing DB lacks (idempotent, no rebuild). The write path became an **idempotent UPSERT** that
sets every disk-derived column from the incoming row but keeps `convergence_reason` via
`COALESCE(excluded, existing)` — so `migrate_runs` is now also a **repair/sync** that backfills new
columns on rows recorded before they existed. Advancing after a schema change: `up -d
--force-recreate backend` → `migrate_runs` (backfills) → `reconcile_runs` CLEAN.

*`cv_final.html` still generated at run time (belt + braces during transition).*

### Phase 3 — Retire the old write paths (built, F-60) — THE IRREVERSIBLE STEP

1. **Remove `cv_final.html` generation from Phase 6** — `generate_output` writes only `cv_final.md`;
   `GET /api/runs/{id}/html` (on-demand `regenerate_html`) is the only HTML path. Existing
   `cv_final.html` files in `outputs/` are kept as snapshots (not deleted).
2. **SQLite is the source of truth for run metadata.** Stop writing `run_meta.json` on new runs.
   Creation-time metadata not reconstructable from disk (company label, Job Radar
   source/assessment/extraction, `rerun_of`, visibility flags) is written to a partial
   `status='running'` row at run creation via **`db.record_run_start`** (from `start_run`/`rerun_run`).
   The Job Radar dicts are JSON `TEXT` columns (`_ADDED_COLUMNS`). `record_run_complete` UPSERTs the
   disk-derived completion columns **over** that row, preserving the CREATION-owned columns via
   `COALESCE(runs.col, excluded.col)` (`_CREATION_OWNED`: `company_name`, `rerun_of`,
   `job_radar_job_id`, the three JSON columns). `public_demo`/`keep` stay `excluded`-wins (always 0 at
   completion; only a post-run PATCH sets 1) so a re-migration still repairs legacy sidecar drift.
3. **`PATCH /{id}/meta` writes SQLite** (`db.update_run_meta`).
4. **One reader at every seam** — `db.get_run_creation_meta` (SQLite-first, sidecar fallback for
   pre-Phase-3 runs) replaces `read_meta` in the Job Radar callback + telemetry (`runner`), the
   owner-gated detail + rerun (`runs`), the report header badge (`phase6`), and **`archive.is_public`
   /`cleanup_runs`** (which gate visibility + retention on the flags — must read SQLite too).
5. **`reconcile_runs` reworked** (§6e): creation-owned fields have no disk source for a Phase-3 run, so
   they are compared only when a sidecar still exists (pre-Phase-3), reported as drift, never gating;
   a `status='running'` row is exempt from the orphan check.
6. **Retire the dead legacy endpoints** `GET /{id}/detail`, `/{id}/report`, `/archive` (+
   `archive.list_runs`/`run_detail`, `api.archiveRuns`/`runDetail`/`reportUrl`) — unused since Phase 2;
   repoint the RunPage "Open report" link to `runHtmlUrl`.
7. Update all `cv_final.html` / `run_meta.json` references in docs + comments.

*`cv_final.md` (submission artifact), `run_log.jsonl` (audit trail), and `outputs/<id>/sections/` are
never touched. Pre-Phase-3 `run_meta.json` sidecars are kept — `migrate_runs` + the read fallback still
use them.*

**Deploy (schema + backend + frontend change; the F-59 force-recreate lesson):** `git pull` → `docker
compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backend frontend` (force-recreate)
→ `migrate_runs` (backfills the new JSON columns + syncs any legacy flag drift) → `reconcile_runs` CLEAN
→ soak → verify a fresh run's visibility/Job-Radar/company, PATCH persistence, no run-time `cv_final.html`,
and the on-demand `/html` download. **Confirm reconcile is CLEAN on prod before considering it done.**

---

## 7. Backward compatibility

- Existing runs without SQLite entries: `cli/migrate_runs.py` backfills
  them. Nulls for fields that didn't exist (e.g. `jd_role_title` for old
  runs).
- The `run_meta.json` **write** is retired in Phase 3 (SQLite is the source of truth), but
  pre-Phase-3 **sidecars are kept on disk** — `migrate_runs` reads them to backfill old runs, and
  `db.get_run_creation_meta` falls back to them per field for a run not yet in SQLite.
- Old `cv_final.html` files in `outputs/` are not deleted — they remain
  as static snapshots (still downloadable via `/{id}/files/cv_final.html`) but are no longer the
  UI surface; new runs don't write one (the report is on-demand).
- CLI `replay` command unchanged — reads JSONL directly.

---

## 8. Alignment with Job Radar

Both systems now share the same storage architecture:

| Layer | Job Radar | cv-tailor |
|---|---|---|
| Structured metadata | `corpus/job_radar.db` (SQLite) | `data/cv_tailor.db` (SQLite) |
| Audit trail | append-only JSONL files | `run_log.jsonl` per run |
| Binary artifacts | source JD files | section .md files, cv_final.md |
| Backup | rsync `corpus/` | rsync `data/` + `outputs/` |

Phase 4 cross-system analysis becomes a SQL query joining both databases
(or a script that reads both). No file parsing, no JSONL wrangling.

---

## 9. Gitignore additions

```
data/cv_tailor.db          # personal run data
data/cv_tailor.db-shm      # SQLite WAL files
data/cv_tailor.db-wal
```

---

## 10. Verification gates

- [ ] `cli/migrate_runs.py` populates SQLite from all existing run files,
      idempotently
- [ ] New runs write to SQLite at `run_complete`
- [ ] `GET /api/runs` returns paginated list sorted by ts DESC
- [ ] `GET /api/runs/{id}` returns structured JSON with all six tab data
- [ ] Run detail page renders all six tabs from API (no static HTML)
- [ ] New field added to `runs` table appears in UI without HTML regeneration
- [ ] `GET /api/runs/{id}/html` generates correct HTML on demand
- [ ] Old runs (pre-migration) render correctly with nulls for new fields
- [ ] `data/cv_tailor.db` is gitignored and bind-mounted
- [ ] Existing test suite passes unchanged
- [ ] `cv_final.md` download still works
- [ ] `cli/reconcile_runs.py` reports CLEAN after the ~1-week soak (the per-phase advance gate)
