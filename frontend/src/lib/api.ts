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

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
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
  filename: string;
  cv_type: string;
  target_role: string;
  seniority: string;
  version_date: string;
  section_count: number;
  sections: CVSection[];
}

export interface StartRunResponse {
  run_id: string;
  mode: string;
  status: string;
}

// Progress events streamed over SSE (SPEC §12.2). `type` discriminates; the rest
// of the fields depend on the type (kept loose — the Run page reads by type).
export interface RunEvent {
  type: string;
  seq?: number;
  [key: string]: unknown;
}

export const api = {
  health: () => get<{ status: string; service: string }>("/health"),
  corpusStats: () => get<CorpusStats>("/corpus/stats"),
  listCVs: () => get<CVItem[]>("/corpus/cvs"),
  deleteCV: (filename: string) =>
    del<{ deleted: string; sections_removed: number }>(
      `/corpus/cvs/${encodeURIComponent(filename)}`,
    ),
  startRun: (jd_text: string, mode: string, key?: string) =>
    post<StartRunResponse>("/runs", { jd_text, mode, key: key || null }),
  runStreamUrl: (runId: string) => `${BASE}/runs/${encodeURIComponent(runId)}/stream`,
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
] as const;
