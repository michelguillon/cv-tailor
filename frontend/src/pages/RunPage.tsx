import { useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Check, Circle, Play, AlertTriangle, Download, ExternalLink, Lock, LockOpen, ChevronRight, ChevronDown } from "lucide-react";
import { api, RUN_EVENT_TYPES, type RunEvent, type HitlReady, type HitlDecision, type JobRadarPrefill, type JobRadarAssessment } from "@/lib/api";
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

export function RunPage({
  attachRunId,
  onAttached,
}: {
  // Re-run handoff (SPEC_RERUN §4.1): when set, attach the progress view to an already-started
  // run's SSE stream instead of starting a new one from the form.
  attachRunId?: string | null;
  onAttached?: () => void;
} = {}) {
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
  // Phase 3: result of the "link back to Job Radar" callback, shown in the run summary.
  const [jobLink, setJobLink] = useState<{ ok: boolean } | null>(null);

  // Full mode is gated on the shared owner unlock (D-38/D-39); the capability state +
  // unlock dialog live in UnlockProvider so the Corpus page reuses the same one unlock.
  const { configured, unlocked, requestUnlock, lock } = useUnlock();
  // Job Radar assessment context (SPEC §12.12) — owner-only, collapsed by default.
  const [assessmentOpen, setAssessmentOpen] = useState(false);
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
  // Transient state: EventSource is auto-retrying a dropped connection (proxy/tunnel blip).
  // Shown as a badge; NOT an error — the backend replays the event buffer on reconnect.
  const [reconnecting, setReconnecting] = useState(false);

  const runIdRef = useRef<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const doneRef = useRef(false);
  // Highest event seq already applied. On reconnect the backend replays from seq 0, so we
  // skip anything <= this to avoid double-appending timeline rows / re-firing HITL panels.
  const lastSeqRef = useRef(-1);
  const jobRadarRef = useRef<{ source: string; job_id: string } | null>(null);
  const linkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    esRef.current?.close();
    if (linkTimerRef.current) clearTimeout(linkTimerRef.current);
  }, []);

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
        const ref = { source: "job_radar", job_id: job.job_id };
        setJobRadar(ref);
        jobRadarRef.current = ref;   // ref mirror — read inside the SSE handler closure
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
    setJobLink(null);
    setReconnecting(false);
    if (linkTimerRef.current) {
      clearTimeout(linkTimerRef.current);
      linkTimerRef.current = null;
    }
    doneRef.current = false;
    lastSeqRef.current = -1;
  }

  function closeStream() {
    if (linkTimerRef.current) {
      clearTimeout(linkTimerRef.current);
      linkTimerRef.current = null;
    }
    esRef.current?.close();
    esRef.current = null;
  }

  function finish() {
    doneRef.current = true;
    setRunning(false);
    setHitl(null);
    setHitlBusy(false);
    closeStream();
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
    // Drop replays after a reconnect: the backend re-streams the whole buffer from seq 0,
    // so anything we've already applied (seq <= lastSeq) must be ignored. Events without a
    // seq (e.g. the `connected` keepalive) bypass this and are handled by the switch below.
    if (typeof ev.seq === "number") {
      if (ev.seq <= lastSeqRef.current) return;
      lastSeqRef.current = ev.seq;
    }
    // Receiving any event means the stream is healthy again.
    if (reconnecting) setReconnecting(false);
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
        setHitlBusy(false);
        setSummary({
          outcome: ev.outcome as string,
          iterations: ev.iterations as number,
          converged: ev.converged as boolean,
          convergence_reason: ev.convergence_reason as string,
          cost: ev.cost_estimated_usd as number,
        });
        // The run is logically done — suppress any later stream-close error and stop the spinner.
        doneRef.current = true;
        setRunning(false);
        // Phase 3: a Job Radar run emits a trailing `job_radar_linked` event AFTER run_complete;
        // keep the stream open briefly to show the ✓/⚠ indicator, then close (grace fallback in
        // case the callback is skipped server-side, e.g. no service key).
        if (jobRadarRef.current) {
          linkTimerRef.current = setTimeout(closeStream, 8000);
        } else {
          closeStream();
        }
        break;
      case "job_radar_linked":
        setJobLink({ ok: Boolean(ev.ok) });
        closeStream();
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

  // Open the SSE stream for a run id and wire the event listeners. Shared by start() (a fresh
  // run) and attach() (a re-run already started server-side, SPEC_RERUN §4.1).
  function openStream(run_id: string) {
    setRunId(run_id);
    runIdRef.current = run_id;
    const es = new EventSource(api.runStreamUrl(run_id));
    esRef.current = es;
    for (const t of RUN_EVENT_TYPES) {
      es.addEventListener(t, (e) => handleEvent(JSON.parse((e as MessageEvent).data) as RunEvent));
    }
    // A (re)opened connection is healthy — clear any "Reconnecting…" badge.
    es.onopen = () => setReconnecting(false);
    es.onerror = () => {
      if (doneRef.current) return;            // run already finished — a normal close, ignore
      // EventSource auto-reconnects on a transient drop (proxy/tunnel blip): readyState is
      // CONNECTING (0) while it retries. Let it heal — just show a transient badge. Only when
      // it gives up (CLOSED = 2) do we surface a hard error. The backend replays the event
      // buffer on reconnect, so the run continues seamlessly (seq-dedup drops the replay).
      if (es.readyState === EventSource.CLOSED) {
        setReconnecting(false);
        setError("Lost connection to the run stream.");
        finish();
      } else {
        setReconnecting(true);
      }
    };
  }

  async function start() {
    reset();
    setRunning(true);
    try {
      const { run_id } = await api.startRun(jd, mode, auto, company.trim() || null, jobRadar);
      openStream(run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
    }
  }

  // Attach to an already-started re-run (SPEC_RERUN §4.1): the backend created it and started the
  // pipeline; we just stream its progress. The SSE buffer replays from seq 0, so a freshly-attached
  // view catches every event already emitted. We don't know locally whether this run carries a Job
  // Radar link, so flag it so run_complete keeps the stream open briefly for a trailing
  // job_radar_linked event (the 8s grace closes it if none arrives).
  useEffect(() => {
    if (!attachRunId) return;
    reset();
    setRunning(true);
    jobRadarRef.current = { source: "rerun", job_id: "" };
    openStream(attachRunId);
    onAttached?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attachRunId]);

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
          {/* Job Radar assessment context (SPEC §12.12): the owner's manual review of the role —
              context, not primary info, so collapsed by default. Owner-only — hidden in the
              public/demo view (the prefill proxy returns null assessment to non-owners anyway). */}
          {unlocked && jobPrefill?.assessment && (
            <JobRadarAssessmentPanel
              assessment={jobPrefill.assessment}
              open={assessmentOpen}
              onToggle={() => setAssessmentOpen((v) => !v)}
            />
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
          {reconnecting && !error && (
            <div className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Reconnecting… (the run continues in the background)
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
            {/* Phase 3: best-effort confirmation that metrics were linked back to Job Radar. */}
            {jobLink && (
              <div className="flex w-full items-center gap-1.5 text-xs">
                {jobLink.ok ? (
                  <span className="text-success">✓ Linked back to Job Radar</span>
                ) : (
                  <span className="text-amber-600 dark:text-amber-500">
                    ⚠ Could not link back to Job Radar — add metrics manually
                  </span>
                )}
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

// The owner's Job Radar assessment of this role (SPEC §12.12) — a collapsible context panel.
// Renders only the fields that are present; the scorer label and the owner's override are shown
// together (`strong_fit → good_fit`) when an override exists. Owner-only — gated by the caller.
function JobRadarAssessmentPanel({
  assessment,
  open,
  onToggle,
}: {
  assessment: JobRadarAssessment;
  open: boolean;
  onToggle: () => void;
}) {
  const a = assessment;
  const scorer =
    a.fit_label != null
      ? `${a.fit_label}${a.fit_score != null ? ` (${a.fit_score})` : ""}`
      : null;
  return (
    <div className="rounded-md border border-border bg-muted/30 text-xs">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-left font-medium text-foreground"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        Job Radar assessment
      </button>
      {open && (
        <dl className="space-y-1.5 border-t border-border px-3 py-2 text-muted-foreground">
          {scorer && (
            <Row label="Scorer">
              {scorer}
              {a.fit_override && (
                <span className="text-foreground"> → override: {a.fit_override.label}</span>
              )}
            </Row>
          )}
          {a.fit_override?.reason && <Row label="Override">“{a.fit_override.reason}”</Row>}
          {a.requirement_gaps.length > 0 && <Row label="Gaps">{a.requirement_gaps.join(", ")}</Row>}
          {a.blocking_constraints.length > 0 && (
            <Row label="Blocked">
              <span className="text-amber-600 dark:text-amber-500">
                {a.blocking_constraints.join(", ")}
              </span>
            </Row>
          )}
          {a.owner_status && <Row label="Status">{a.owner_status}</Row>}
          {a.annotations.length > 0 && (
            <div className="pt-1">
              <div className="font-medium text-foreground">Annotations</div>
              {a.annotations.map((an, i) => (
                <div key={i} className="pl-2">
                  {an.field ?? an.type}: “{an.reason}”
                </div>
              ))}
            </div>
          )}
          {a.notes.length > 0 && (
            <div className="pt-1">
              <div className="font-medium text-foreground">Notes</div>
              {a.notes.map((n, i) => (
                <div key={i} className="pl-2">
                  {n.ts ? `${n.ts.slice(0, 10)}  ` : ""}“{n.text}”
                </div>
              ))}
            </div>
          )}
        </dl>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="w-20 shrink-0 font-medium text-foreground">{label}</dt>
      <dd className="flex-1">{children}</dd>
    </div>
  );
}
