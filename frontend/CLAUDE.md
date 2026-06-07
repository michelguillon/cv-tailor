# CLAUDE.md — frontend/ (React + Vite + shadcn-style UI, SPEC §12)

The portfolio demo surface. Talks only to the FastAPI backend over `/api/*`. Read
the root `CLAUDE.md` first. Built incrementally per SPEC §12.6 (UI Steps 2–6).

## Stack & conventions

- **React 18 + Vite + TypeScript + Tailwind v3**, shadcn-style components
  hand-written under `src/components/ui/` (cva + `cn()` from `src/lib/utils.ts`).
  No `shadcn init` step — the primitives are copied/owned here, same as shadcn’s model.
- **All network calls go through `src/lib/api.ts`** — one typed client; never `fetch`
  inline in a component. Types mirror the backend response shapes (keep them in sync
  when an endpoint changes).
- **Same-origin `/api`** — the Vite dev server proxies `/api` → `http://backend:8000`
  (`vite.config.ts`), so there’s no CORS in dev and the same code works behind nginx
  in prod (which serves the bundle and proxies `/api`).
- **Pages under `src/pages/`**, one per mode (Corpus, Run, …); `App.tsx` is the shell
  + tab nav. Keep pages thin: fetch via `api`, render with `ui/` primitives.
- **Owner unlock (D-38/D-39, F-44/F-45):** the capability state + the one unlock dialog live
  in `components/UnlockProvider.tsx` (wraps the app in `App.tsx`), which owns
  `api.capabilities()` and exposes `useUnlock()` → `{caps, configured, unlocked, requestUnlock,
  lock, refresh}`. `requestUnlock()` opens the dialog if needed and resolves `true` once
  unlocked (immediately if already unlocked) / `false` on cancel — callers gate an action on
  it. ONE signed HttpOnly cookie (sent automatically, same-origin) authorises **both** full
  (Sonnet) runs (§12.7) **and** corpus writes (§12.8). `RunPage` uses it for the mode picker
  (`full` shown only when `configured`; selecting it calls `requestUnlock`). `CorpusPage` uses
  it to gate Add/Edit/Replace/Delete (controls hidden when `!configured` = read-only deploy;
  shown + open the prompt when locked). The raw key is **never** kept in state after submit;
  `startRun` sends no key. Backend is the source of truth (403); UI gating is convenience only.
- **Output panel summary card (D-34/F-43):** `OutputPanel` renders a summary card —
  fit band + %, grounded coverage, unsupported claims (⚠ when >0), derived status — from
  the run's archive fields (`grounded_coverage`, `unsupported_claims`, `status`,
  `fit_band`); the embedded `cv_final.html` iframe carries its own sticky card + the JD
  tab, so there's no separate React JD view. The card numbers come from existing engine
  signals (coverage F-38 + verifier flags F-35), not a new pass.
- **Corpus write path (D-36/F-42):** `CorpusPage` opens `CvWizard` (Add + Replace,
  4 steps: upload → metadata form → section-inventory gate → confirm) and
  `EditMetadataDialog` (one step, no inventory) over the `ui/dialog.tsx` modal.
  Both render `CvMetadataForm`, which owns the chip input and exports
  `validateMetadata` — the field rules **mirror the backend `validate_sidecar`**
  (the 422 is a safety net, not the primary UX); keep them in sync. File upload
  uses `api.postForm` (multipart, no JSON Content-Type); `ApiError.status` lets the
  wizard branch on **409** (duplicate → "use Replace"). Ingest is synchronous (a
  spinner, not SSE) — the human gate is the inventory step, not progress. Every write
  action is gated on the owner unlock via `useUnlock().requestUnlock()` (D-39/§12.8);
  the controls are hidden on a read-only deployment (`!configured`).
- **SSE (UI Step 3+):** consume `/api/runs/{id}/stream` with `EventSource`; render the
  progress timeline + inline HITL panels as events arrive. `proxy_buffering off` in
  nginx (prod) is required or SSE buffers (SPEC §7.5).
- **Conversational HITL (UI Step 4, F-31):** a `hitl_ready` event drives
  `components/HitlPanel.tsx` (one inline panel per checkpoint — fit / section review /
  formatting); the human's decision is `api.submitHitl(runId, {action, …})`. The fit
  panel renders a **"Why you're a fit"** callout from `payload.value_alignment_notes`
  (CVCM, F-39) when present; the same summary persists in the report's **Fit tab** for
  `--yes`/auto runs that never pause here (the OutputPanel iframe shows it). The panel
  is hidden on submit and re-driven by the next SSE event (the review loop re-publishes
  `hitl_ready` with a `payload.preview` for confirm-apply). `hitl_interpreted` /
  `hitl_applied` / `hitl_error` are surfaced as a small log. New event types must be
  added to `RUN_EVENT_TYPES` in `api.ts` or their listeners aren't registered.

## Run (Docker)

`docker compose up frontend backend` → http://localhost:3000 (frontend), :8000 (API).
The image bakes `node_modules`; compose bind-mounts the source and preserves
`node_modules` via an anonymous volume. Verify a change compiles with
`docker compose run --rm frontend npm run build` (tsc + vite build) — there’s no
browser in CI, so the production build is the type/compile gate.
