# SPEC_RERUN.md
## cv-tailor — Re-run from Existing Run

**Status:** Built (F-57)  
**Scope:** Run detail page button + backend endpoint + Job Radar callback  
**Depends on:** existing `job_radar_source` metadata, existing Phase 3 callback

> **Implementation note (F-57).** Two details differ from the illustrative JSON below, resolved
> against the actual architecture: (1) `jd_raw` is **not** stored in `run_meta.json` — the raw JD
> already lives in `outputs/<run_id>/jd_raw.txt` (the report's JD-tab source), so the endpoint
> reads it from there; "no stored JD → 400" maps to a missing `jd_raw.txt`. (2) `rerun_of` is a
> write-once sidecar key (like `job_radar_source`) but is **absent** from `default_meta()` — an
> original run has no key and consumers read `.get("rerun_of")` → None, which keeps the
> sidecar-less default contract (and its test) unchanged.

---

## 1. What it does

A **Re-run** button on the run detail page creates a new run pre-populated
with the same JD, carries the `job_radar_source` reference forward, and lets
the user choose full or demo mode. On completion the Phase 3 callback fires
to Job Radar exactly as a fresh run would — appending a new record to
`cv_tailor_links` so the latest-per-job logic picks up the updated scores.

---

## 2. Data flow

```
Run detail page
  [Re-run] button
    ↓
POST /api/runs/{original_run_id}/rerun
  body: { mode: "full" | "demo" }
    ↓
Backend:
  1. Load original run metadata (jd_raw, job_radar_source)
  2. Create new run with:
       jd_raw = original.jd_raw
       job_radar_source = original.job_radar_source  ← lineage carried forward
       rerun_of = original_run_id                    ← audit trail
       mode = requested mode
  3. Start pipeline (same as POST /api/runs)
    ↓
Pipeline completes → Phase 3 callback fires to Job Radar
  (if job_radar_source present — same logic as original run)
    ↓
Job Radar appends new record to cv_tailor_links
Latest-per-job logic shows updated scores
```

---

## 3. Backend

### 3.1 New endpoint

```
POST /api/runs/{original_run_id}/rerun
```

**Auth:** owner-gated (same capability cookie as all write endpoints)

**Body:**
```json
{ "mode": "full" | "demo" }
```

**Logic:**
1. Load `run_meta.json` from `outputs/{original_run_id}/`
2. Verify original run exists and belongs to owner — 404 if not found
3. Extract `jd_raw` and `job_radar_source` from original metadata
4. Create new run directory with new `run_id`
5. Write `run_meta.json` for new run:
   ```json
   {
     "run_id": "run_20260614_143000",
     "mode": "full",
     "jd_raw": "<copied from original>",
     "job_radar_source": "<copied from original, may be null>",
     "rerun_of": "<original_run_id>",
     "public_demo": false,
     "keep": false
   }
   ```
6. Start pipeline in background (same `asyncio.to_thread` pattern)
7. Return `{"run_id": "<new_run_id>"}` immediately
   — client redirects to the new run's SSE stream

**Error cases:**
- Original run not found → 404
- Original run has no `jd_raw` (very old run before jd_raw was stored) →
  400 `{"detail": "Original run has no stored JD — re-run not possible"}`

### 3.2 `rerun_of` field

Add `rerun_of: str | None` to `run_meta.json` schema. `None` for original
runs, `original_run_id` for re-runs. Used for audit trail only — no
functional pipeline dependency.

---

## 4. Frontend — run detail page

### 4.1 Re-run button

- Location: run detail page header, alongside existing Download / Delete controls
- Owner-only: hidden when not unlocked
- Label: **Re-run**
- On click: show a small modal (not a full page):

```
┌─────────────────────────────────────┐
│  Re-run this tailoring              │
│                                     │
│  JD: [role title from run metadata] │
│                                     │
│  Mode:  ○ Demo   ● Full             │
│                                     │
│  [Cancel]        [Start Re-run]     │
└─────────────────────────────────────┘
```

- Default mode: full (the most likely reason to re-run is to upgrade from demo)
- [Start Re-run] → POST /api/runs/{id}/rerun → redirect to new run's page
  (same as starting a fresh run — user lands on the SSE progress view)

### 4.2 Re-run provenance badge

On the new run's detail page, show a small provenance note in the header:

```
Re-run of run_20260613_111401
```

Linked to the original run's detail page. Derived from `rerun_of` in
`run_meta.json`.

---

## 5. Job Radar callback

No changes needed to the callback logic itself. The existing Phase 3
callback already reads `job_radar_source` from `run_meta.json` and fires
if present. Since the re-run copies `job_radar_source` from the original,
the callback fires automatically on completion.

The new record appended to Job Radar's `cv_tailor_links`:
```json
{
  "run_id": "run_20260614_143000",
  "ts": "2026-06-14T14:35:22Z",
  "mode": "full",
  "fit_score": 0.81,
  "coverage": 0.86,
  "quality": 8.7,
  "rerun_of": "run_20260613_111401"
}
```

Include `rerun_of` in the callback payload so Job Radar can display
lineage if it wants to (optional — Job Radar can ignore the field without
breaking).

Job Radar's latest-per-job logic already picks the most recent record by
`ts` — no changes needed there.

---

## 6. Verification gates

- [ ] Re-run button visible on run detail page when owner-unlocked, hidden otherwise
- [ ] Modal shows role title (or truncated JD if no title), defaults to full mode
- [ ] POST /api/runs/{id}/rerun creates new run with correct `jd_raw`,
      `job_radar_source`, and `rerun_of` fields
- [ ] New run's detail page shows provenance badge linking to original
- [ ] Pipeline runs to completion with new run_id
- [ ] If original has `job_radar_source`: Phase 3 callback fires to Job Radar
      with new run's scores + `rerun_of` field
- [ ] If original has no `job_radar_source`: no callback, run completes normally
- [ ] 400 returned cleanly if original run has no `jd_raw`
- [ ] Existing runs without `rerun_of` field render correctly (null = no badge)
- [ ] Existing test suite passes unchanged
