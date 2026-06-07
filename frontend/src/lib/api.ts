// Typed client for the FastAPI backend (SPEC §12.5). All calls are same-origin
// /api/* — the Vite dev server proxies them to the backend container.

const BASE = "/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// Raised when an HTTP call fails; carries the status so callers can branch (e.g. 409).
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const j = (await res.json()) as { detail?: string };
    if (j.detail) detail = j.detail;
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(res.status, detail);
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await errorFrom(res);
  return res.json() as Promise<T>;
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await errorFrom(res);
  return res.json() as Promise<T>;
}

// Multipart POST (file upload) — no JSON Content-Type; the browser sets the boundary.
async function postForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "POST", body: form });
  if (!res.ok) throw await errorFrom(res);
  return res.json() as Promise<T>;
}

export interface CorpusStats {
  cv_count: number;
  section_count: number;
  by_section_type: Record<string, number>;
  last_ingested: string | null;
}

export interface CVSection {
  section_id: string;
  section_type: string;
  word_count: number;
  static: boolean;
  title: string;
}

export interface CVItem {
  filename: string;        // real corpus key (used for delete); not shown to the user
  display_name: string;    // company-name-free label shown in the UI (F-41)
  cv_type: string;
  target_role: string;
  target_company: string | null;
  seniority: string;
  version_date: string;
  section_count: number;
  sections: CVSection[];
}

// The editorial metadata the YAML form collects (D-36). Mirrors the sidecar fields;
// the backend validates the same way load_sidecar does.
export interface CvMetadataFields {
  filename: string;
  cv_type: "generic" | "job_specific";
  target_role: string;
  target_company: string | null;
  skills_emphasis: string[];
  seniority: "senior" | "principal" | "director" | "vp";
  version_date: string;
}

// One row of the parse preview — the R-01 section-inventory gate (D-36).
export interface SectionPreview {
  section_id: string;
  section_type: string;
  word_count: number;
  static: boolean;
  title: string;
}

export interface UploadPreview {
  token: string;             // staged-upload handle, passed back to /confirm
  filename: string;
  replace: boolean;
  sections: SectionPreview[];
  section_count: number;
  below_minimum: boolean;    // < MIN_SECTIONS → likely a silent parse failure (warn, R-01)
  min_sections: number;
  warnings: string[];
  empty_headers: string[];
}

export interface ConfirmResult {
  status: string;
  filename: string;
  sections_committed: number;
  removed: number;           // sections of the prior version dropped (Replace)
  replaced: boolean;
}

export interface StartRunResponse {
  run_id: string;
  mode: string;
  status: string;
}

// Full Mode Unlock Gate (D-38). The UI renders the mode picker from this; full mode is
// reachable only when configured server-side and unlocked (a signed HttpOnly cookie the
// browser sends automatically — the raw key never lives in the frontend).
export interface Capabilities {
  demo_available: boolean;
  full_configured: boolean;
  full_unlocked: boolean;
}

export interface ArchiveRun {
  run_id: string;
  mode: string | null;
  role_title: string | null;
  outcome: string | null;
  fit_score: number | null;
  iterations: number | null;
  cost_estimated_usd: number | null;
  cost_breakdown?: Record<string, number> | null;
  // Summary card (D-34): grounded coverage (F-38) + verifier flags (F-35) + derived status.
  grounded_coverage: number | null;
  unsupported_claims: number | null;
  status: string | null;
  fit_band: string | null;
  has_md: boolean;
  has_html: boolean;
}

export interface RunDetail extends ArchiveRun {
  iteration_scores: Array<Record<string, unknown>>;
  reasoning: Array<Record<string, unknown>>;
  cv_md: string | null;
}

// Progress events streamed over SSE (SPEC §12.2). `type` discriminates; the rest
// of the fields depend on the type (kept loose — the Run page reads by type).
export interface RunEvent {
  type: string;
  seq?: number;
  [key: string]: unknown;
}

// Conversational HITL (SPEC §12.3). `hitl_ready` carries the checkpoint + a payload;
// the human's decision is POSTed back as a HitlDecision. Payload fields are read by
// the HitlPanel per checkpoint, so they're kept loose here.
export type HitlCheckpoint = "fit_assessment" | "section_review" | "formatting";

export interface HitlReady {
  checkpoint: HitlCheckpoint;
  payload: Record<string, unknown>;
}

export interface HitlDecision {
  action: string;
  text?: string;
  index?: number;
  section_id?: string;
  instruction?: string;
}

export const api = {
  health: () => get<{ status: string; service: string }>("/health"),
  corpusStats: () => get<CorpusStats>("/corpus/stats"),
  listCVs: () => get<CVItem[]>("/corpus/cvs"),
  deleteCV: (filename: string) =>
    del<{ deleted: string; sections_removed: number }>(
      `/corpus/cvs/${encodeURIComponent(filename)}`,
    ),
  cvMetadata: (filename: string) =>
    get<CvMetadataFields>(`/corpus/cvs/${encodeURIComponent(filename)}/metadata`),
  uploadCV: (file: File, metadata: CvMetadataFields, replace = false) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("metadata", JSON.stringify(metadata));
    form.append("replace", String(replace));
    return postForm<UploadPreview>(replace ? "/corpus/replace" : "/corpus/upload", form);
  },
  confirmCV: (body: { token: string; filename: string; metadata: CvMetadataFields; replace: boolean }) =>
    post<ConfirmResult>("/corpus/confirm", body),
  editMetadata: (filename: string, metadata: CvMetadataFields) =>
    patch<{ status: string; filename: string; sections_updated: number }>(
      `/corpus/cvs/${encodeURIComponent(filename)}/metadata`,
      { metadata },
    ),
  startRun: (jd_text: string, mode: string, auto = false) =>
    post<StartRunResponse>("/runs", { jd_text, mode, auto }),
  capabilities: () => get<Capabilities>("/capabilities"),
  unlockFullMode: (key: string) => post<{ unlocked: boolean }>("/full-mode/unlock", { key }),
  lockFullMode: () => post<{ unlocked: boolean }>("/full-mode/lock", {}),
  submitHitl: (runId: string, body: HitlDecision) =>
    post<{ ok: boolean; status: string }>(`/runs/${encodeURIComponent(runId)}/hitl`, body),
  runStreamUrl: (runId: string) => `${BASE}/runs/${encodeURIComponent(runId)}/stream`,
  archiveRuns: () => get<ArchiveRun[]>("/runs/archive"),
  runDetail: (runId: string) => get<RunDetail>(`/runs/${encodeURIComponent(runId)}/detail`),
  reportUrl: (runId: string) => `${BASE}/runs/${encodeURIComponent(runId)}/report`,
  fileUrl: (runId: string, name: string) =>
    `${BASE}/runs/${encodeURIComponent(runId)}/files/${encodeURIComponent(name)}`,
};

// The progress events the Run page reacts to (also used to register SSE listeners).
export const RUN_EVENT_TYPES = [
  "phase_start",
  "phase_complete",
  "section_update",
  "iteration_complete",
  "run_complete",
  "stopped",
  "error",
  "hitl_ready",        // pipeline paused — render a checkpoint panel
  "hitl_interpreted",  // Haiku read free text — shown back before applying
  "hitl_applied",      // a section was revised
  "hitl_error",        // an action could not be applied (re-published, try again)
] as const;
