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
- **Corpus write path (D-36/F-42):** `CorpusPage` opens `CvWizard` (Add + Replace,
  4 steps: upload → metadata form → section-inventory gate → confirm) and
  `EditMetadataDialog` (one step, no inventory) over the `ui/dialog.tsx` modal.
  Both render `CvMetadataForm`, which owns the chip input and exports
  `validateMetadata` — the field rules **mirror the backend `validate_sidecar`**
  (the 422 is a safety net, not the primary UX); keep them in sync. File upload
  uses `api.postForm` (multipart, no JSON Content-Type); `ApiError.status` lets the
  wizard branch on **409** (duplicate → "use Replace"). Ingest is synchronous (a
  spinner, not SSE) — the human gate is the inventory step, not progress.
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
