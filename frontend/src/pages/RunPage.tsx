import { useEffect, useRef, useState } from "react";
import { Loader2, Check, Circle, Play, AlertTriangle, Download, ExternalLink } from "lucide-react";
import { api, RUN_EVENT_TYPES, type RunEvent } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
  const [mode, setMode] = useState<"demo" | "full">("demo");
  const [key, setKey] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  const [phaseStatus, setPhaseStatus] = useState<Record<string, PhaseStatus>>({});
  const [phaseNote, setPhaseNote] = useState<Record<string, string>>({});
  const [iterations, setIterations] = useState<IterationRow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);

  const esRef = useRef<EventSource | null>(null);
  const doneRef = useRef(false);

  useEffect(() => () => esRef.current?.close(), []);

  function reset() {
    setError(null);
    setSummary(null);
    setIterations([]);
    setPhaseStatus({});
    setPhaseNote({});
    doneRef.current = false;
  }

  function finish() {
    doneRef.current = true;
    setRunning(false);
    esRef.current?.close();
    esRef.current = null;
  }

  function handleEvent(ev: RunEvent) {
    switch (ev.type) {
      case "phase_start":
        setPhaseStatus((p) => ({ ...p, [ev.phase as string]: "running" }));
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
      case "run_complete":
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
      const { run_id } = await api.startRun(jd, mode, mode === "full" ? key : undefined);
      setRunId(run_id);
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
          <textarea
            value={jd}
            onChange={(e) => setJd(e.target.value)}
            placeholder="Paste the job description here…"
            disabled={running}
            className="h-44 w-full resize-y rounded-md border border-border bg-background p-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
          />
          <div className="flex flex-wrap items-center gap-3">
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as "demo" | "full")}
              disabled={running}
              className="h-9 rounded-md border border-border bg-background px-3 text-sm"
            >
              <option value="demo">demo (Haiku, ~$0.10)</option>
              <option value="full">full (Sonnet, key-gated)</option>
            </select>
            {mode === "full" && (
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="FULL_MODE_KEY"
                disabled={running}
                className="h-9 rounded-md border border-border bg-background px-3 text-sm"
              />
            )}
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
