import { useEffect, useRef, useState } from "react";
import { Loader2, Check, Circle, Play, AlertTriangle, Download, ExternalLink, Lock, LockOpen } from "lucide-react";
import { api, RUN_EVENT_TYPES, type RunEvent, type HitlReady, type HitlDecision, type JobRadarPrefill } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { HitlPanel } from "@/components/HitlPanel";
import { useUnlock } from "@/components/UnlockProvider";
import { cn } from "@/lib/utils";

const PHASES: { id: string; label: string }[] = [
  { id: "phase0_jd_analysis", label: "JD analysis" },
  { id: "phase1_fit_assessment", label: "Fit assessment" },
  { id: "phase2_initial_draft", label: "Initial draft" },
  { id: "phase3_refinement", label: "Refinement loop" },
  { id: "phase4_hitl", label: "Human review" },
  { id: "phase5_validation", label: "Formatting" },
  { id: "phase6_output", label: "Output generation" },
];

type PhaseStatus = "pending" | "running" | "done";

interface IterationRow {
  iteration: number;
  coverage: number;
  quality: number | null;
  frozen: number;
  active: number;
}

interface Summary {
  outcome?: string;
  iterations?: number;
  converged?: boolean;
  convergence_reason?: string;
  cost?: number;
}

export function RunPage() {
  const [jd, setJd] = useState("");
  const [company, setCompany] = useState(""); // optional run label (§12.9) → run metadata
  const [mode, setMode] = useState<"demo" | "full">("demo");
  const [auto, setAuto] = useState(false);

  // Job Radar handoff (Integration §5.2): opened with ?source=job_radar&job_id=<id>, the JD is
  // fetched (server-side, via the backend proxy) and the run carries the immutable reference.
  const [jobRadar, setJobRadar] = useState<{ source: string; job_id: string } | null>(null);
  const [jobPrefill, setJobPrefill] = useState<JobRadarPrefill | null>(null);
  const [jobLoading, setJobLoading] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);

  // Full mode is gated on the shared owner unlock (D-38/D-39); the capability state +
  // unlock dialog live in UnlockProvider so the Corpus page reuses the same one unlock.
  const { configured, requestUnlock, lock } = useUnlock();
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  const [phaseStatus, setPhaseStatus] = useState<Record<string, PhaseStatus>>({});
  const [phaseNote, setPhaseNote] = useState<Record<string, string>>({});
  const [iterations, setIterations] = useState<IterationRow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [hitl, setHitl] = useState<HitlReady | null>(null);
  const [hitlBusy, setHitlBusy] = useState(false);
  const [hitlLog, setHitlLog] = useState<string[]>([]);

  const runIdRef = useRef<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const doneRef = useRef(false);

  useEffect(() => () => esRef.current?.close(), []);

  // On mount: if launched from Job Radar (?source=job_radar&job_id=<id>), fetch the JD through
  // the backend proxy (no direct cross-origin call — avoids CORS) and pre-fill the form. Strip
  // the params (replace, don't push) so a refresh doesn't re-trigger. Fail gracefully: on any
  // error, fall back to manual paste — never block the page (Integration §5.2).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("source") !== "job_radar") return;
    const jobId = params.get("job_id");
    const url = new URL(window.location.href);
    url.searchParams.delete("source");
    url.searchParams.delete("job_id");
    window.history.replaceState({}, "", url.toString());
    if (!jobId) return;

    setJobLoading(true);
    setJobError(null);
    api
      .jobRadarPrefill(jobId)
      .then((job) => {
        if (!job.raw_text.trim()) {
          setJobError("Could not load job from Job Radar — paste the JD manually.");
          return;
        }
        setJd(job.raw_text);
        if (job.company) setCompany(job.company);
        setJobPrefill(job);
        setJobRadar({ source: "job_radar", job_id: job.job_id });
      })
      .catch(() => setJobError("Could not load job from Job Radar — paste the JD manually."))
      .finally(() => setJobLoading(false));
  }, []);

  function onPickMode(value: "demo" | "full") {
    if (value === "demo") {
      setMode("demo");
      return;
    }
    // Selecting full opens the shared unlock dialog if needed; stay in demo if cancelled.
    void (async () => {
      if (await requestUnlock()) setMode("full");
    })();
  }

  async function lockFull() {
    await lock();
    setMode("demo");
  }

  function reset() {
    setError(null);
    setSummary(null);
    setIterations([]);
    setPhaseStatus({});
    setPhaseNote({});
    setHitl(null);
    setHitlBusy(false);
    setHitlLog([]);
    doneRef.current = false;
  }

  function finish() {
    doneRef.current = true;
    setRunning(false);
    setHitl(null);
    setHitlBusy(false);
    esRef.current?.close();
    esRef.current = null;
  }

  async function decide(d: HitlDecision) {
    const rid = runIdRef.current;
    if (!rid) return;
    setHitlBusy(true); // keep the panel visible (greyed, "Sending…") until the SSE resolves it
    try {
      await api.submitHitl(rid, d);
    } catch (e) {
      setHitlLog((l) => [...l, `Failed to send: ${e instanceof Error ? e.message : String(e)}`]);
      setHitlBusy(false);
    }
  }

  function handleEvent(ev: RunEvent) {
    switch (ev.type) {
      case "phase_start":
        setPhaseStatus((p) => ({ ...p, [ev.phase as string]: "running" }));
        // Advancing to a new phase means any prior checkpoint is resolved — clear its
        // panel. A within-phase checkpoint (review/formatting) re-appears via hitl_ready,
        // which is emitted *after* its phase_start, so this never hides a live panel.
        setHitl(null);
        setHitlBusy(false);
        break;
      case "phase_complete": {
        const phase = ev.phase as string;
        setPhaseStatus((p) => ({ ...p, [phase]: "done" }));
        if (phase === "phase1_fit_assessment")
          setPhaseNote((n) => ({ ...n, [phase]: `${ev.outcome} · fit ${ev.fit_score}` }));
        if (phase === "phase0_jd_analysis")
          setPhaseNote((n) => ({ ...n, [phase]: String(ev.role_title ?? "") }));
        break;
      }
      case "iteration_complete":
        setIterations((rows) => [
          ...rows,
          {
            iteration: ev.iteration as number,
            coverage: ev.keyword_coverage as number,
            quality: (ev.quality as number | null) ?? null,
            frozen: ev.frozen as number,
            active: ev.active as number,
          },
        ]);
        break;
      case "hitl_ready":
        setHitl({ checkpoint: ev.checkpoint as HitlReady["checkpoint"], payload: ev.payload as Record<string, unknown> });
        setHitlBusy(false);
        break;
      case "hitl_interpreted":
        if (ev.checkpoint === "fit_assessment")
          setHitlLog((l) => [...l, `Got it — ${String(ev.action)}${ev.reason ? ` (${String(ev.reason)})` : ""}.`]);
        break;
      case "hitl_applied":
        setHitlLog((l) => [...l, `Revised ${String(ev.label ?? ev.section_id)} → v${String(ev.version)}.`]);
        break;
      case "hitl_error":
        setHitlLog((l) => [...l, `Couldn't apply that: ${String(ev.message)}`]);
        setHitlBusy(false);
        break;
      case "run_complete":
        setHitl(null);
        setSummary({
          outcome: ev.outcome as string,
          iterations: ev.iterations as number,
          converged: ev.converged as boolean,
          convergence_reason: ev.convergence_reason as string,
          cost: ev.cost_estimated_usd as number,
        });
        finish();
        break;
      case "stopped":
        setError(`Stopped: ${ev.message ?? ev.outcome ?? "pipeline stopped"}`);
        finish();
        break;
      case "error":
        setError(String(ev.message ?? "run failed"));
        finish();
        break;
    }
  }

  async function start() {
    reset();
    setRunning(true);
    try {
      const { run_id } = await api.startRun(jd, mode, auto, company.trim() || null, jobRadar);
      setRunId(run_id);
      runIdRef.current = run_id;
      const es = new EventSource(api.runStreamUrl(run_id));
      esRef.current = es;
      for (const t of RUN_EVENT_TYPES) {
        es.addEventListener(t, (e) => handleEvent(JSON.parse((e as MessageEvent).data) as RunEvent));
      }
      es.onerror = () => {
        if (!doneRef.current) {
          setError("Lost connection to the run stream.");
          finish();
        }
      };
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">Tailor a CV</h2>
        <p className="text-sm text-muted-foreground">
          Paste a job description and watch the multi-model pipeline run, phase by phase.
        </p>
      </div>

      <Card>
        <CardContent className="space-y-4 pt-6">
          {jobLoading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading job from Job Radar…
            </div>
          )}
          {jobError && (
            <div className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-500">
              <AlertTriangle className="h-4 w-4 shrink-0" /> {jobError}
            </div>
          )}
          {jobPrefill && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                From Job Radar: <span className="font-medium text-foreground">{jobPrefill.company ?? "job"}</span>
                {jobPrefill.fit_label && ` — ${jobPrefill.fit_label}`}
                {jobPrefill.fit_score != null && ` (${jobPrefill.fit_score})`}
              </span>
              {jobPrefill.source_url && (
                <a
                  href={jobPrefill.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                >
                  <ExternalLink className="h-3 w-3" /> view role
                </a>
              )}
            </div>
          )}
          <textarea
            value={jd}
            onChange={(e) => setJd(e.target.value)}
            placeholder="Paste the job description here…"
            disabled={running || jobLoading || !!jobRadar}
            className="h-44 w-full resize-y rounded-md border border-border bg-background p-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
          />
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="text"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              placeholder="Company (optional)"
              disabled={running}
              className="h-9 rounded-md border border-border bg-background px-3 text-sm"
            />
            <select
              value={mode}
              onChange={(e) => onPickMode(e.target.value as "demo" | "full")}
              disabled={running}
              className="h-9 rounded-md border border-border bg-background px-3 text-sm"
            >
              <option value="demo">demo (Haiku, ~$0.10)</option>
              {/* full is offered only when configured server-side (D-38); selecting it
                  opens the unlock dialog unless this session is already unlocked. */}
              {configured && <option value="full">full (Sonnet · restricted)</option>}
            </select>
            {mode === "full" ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-success">
                <LockOpen className="h-3.5 w-3.5" /> full unlocked
                <button
                  type="button"
                  onClick={() => void lockFull()}
                  disabled={running}
                  className="ml-1 text-muted-foreground underline-offset-2 hover:underline"
                >
                  lock
                </button>
              </span>
            ) : (
              configured && (
                <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Lock className="h-3.5 w-3.5" /> full mode locked
                </span>
              )
            )}
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={auto}
                onChange={(e) => setAuto(e.target.checked)}
                disabled={running}
                className="h-4 w-4 rounded border-border"
              />
              Auto-run (skip my review)
            </label>
            <Button onClick={() => void start()} disabled={running || !jd.trim()}>
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {running ? "Running…" : "Start"}
            </Button>
          </div>
          {error && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" /> {error}
            </div>
          )}
        </CardContent>
      </Card>

      {(running || summary) && (
        <Card>
          <CardHeader className="py-4">
            <CardTitle className="text-base">Pipeline</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {PHASES.map((p) => {
              const st = phaseStatus[p.id] ?? "pending";
              return (
                <div key={p.id} className="flex items-center gap-3 text-sm">
                  <PhaseIcon status={st} />
                  <span className={cn(st === "pending" && "text-muted-foreground")}>{p.label}</span>
                  {phaseNote[p.id] && (
                    <span className="text-xs text-muted-foreground">— {phaseNote[p.id]}</span>
                  )}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      {hitlLog.length > 0 && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {hitlLog.map((m, i) => (
            <div key={i}>· {m}</div>
          ))}
        </div>
      )}

      {hitl && <HitlPanel checkpoint={hitl.checkpoint} payload={hitl.payload} busy={hitlBusy} onDecide={(d) => void decide(d)} />}

      {iterations.length > 0 && (
        <Card>
          <CardHeader className="py-4">
            <CardTitle className="text-base">Refinement iterations</CardTitle>
          </CardHeader>
          <CardContent>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="pb-1 font-medium">Iter</th>
                  <th className="pb-1 font-medium">Coverage</th>
                  <th className="pb-1 font-medium">Quality</th>
                  <th className="pb-1 font-medium">Frozen</th>
                  <th className="pb-1 font-medium">Active</th>
                </tr>
              </thead>
              <tbody>
                {iterations.map((it) => (
                  <tr key={it.iteration} className="border-t border-border tabular-nums">
                    <td className="py-1.5">{it.iteration}</td>
                    <td className="py-1.5">{(it.coverage * 100).toFixed(0)}%</td>
                    <td className="py-1.5">{it.quality === null ? "—" : it.quality.toFixed(2)}</td>
                    <td className="py-1.5">{it.frozen}</td>
                    <td className="py-1.5">{it.active}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {summary && (
        <Card>
          <CardHeader className="py-4">
            <CardTitle className="text-base">Run complete</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3 text-sm">
            <Badge variant={summary.outcome === "strong" ? "success" : "secondary"}>
              {summary.outcome}
            </Badge>
            <span className="text-muted-foreground">
              {summary.converged ? "converged" : "stopped at cap"}
              {summary.convergence_reason ? ` (${summary.convergence_reason})` : ""} ·{" "}
              {summary.iterations} iteration(s)
            </span>
            <span className="ml-auto font-medium tabular-nums">
              ${summary.cost?.toFixed(4)} <span className="text-muted-foreground">est.</span>
            </span>
            {runId && (
              <div className="flex w-full flex-wrap gap-2 pt-1">
                <a href={api.reportUrl(runId)} target="_blank" rel="noreferrer">
                  <Button variant="outline" size="sm">
                    <ExternalLink className="h-4 w-4" /> Open report
                  </Button>
                </a>
                <a href={api.fileUrl(runId, "cv_final.md")}>
                  <Button variant="ghost" size="sm">
                    <Download className="h-4 w-4" /> cv_final.md
                  </Button>
                </a>
                <span className="self-center text-xs text-muted-foreground">
                  (also under the Runs tab)
                </span>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function PhaseIcon({ status }: { status: PhaseStatus }) {
  if (status === "running") return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  if (status === "done") return <Check className="h-4 w-4 text-success" />;
  return <Circle className="h-4 w-4 text-muted-foreground/40" />;
}
