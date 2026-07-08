import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowLeft, Download, ExternalLink, RotateCcw, Loader2 } from "lucide-react";
import {
  api,
  type RunDetailV2,
  type SectionDiff,
  type ReasoningEntry,
  type RunSection,
} from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { JobRadarAssessmentPanel } from "@/components/JobRadarAssessmentPanel";
import { useUnlock } from "@/components/UnlockProvider";

const FIT_BAND_VARIANT: Record<string, "success" | "secondary" | "destructive"> = {
  strong: "success",
  partial: "secondary",
  low: "destructive",
};

const TABS = ["Fit", "CV", "Changes", "Scores", "Reasoning", "JD"] as const;
type Tab = (typeof TABS)[number];

/** View one completed run: the D-34 summary card, downloads, and the six report tabs
 *  (Fit / CV / Changes / Scores / Reasoning / JD) rendered natively from the structured
 *  API (SPEC_SQLITE_MIGRATION §5.1) — no static HTML iframe. New DB fields surface here
 *  automatically. Owner can Re-run it (SPEC_RERUN §4) and follow re-run lineage. */
export function OutputPanel({
  runId,
  onBack,
  onOpenRun,
  onRerun,
}: {
  runId: string;
  onBack: () => void;
  onOpenRun?: (runId: string) => void;
  onRerun?: (runId: string) => void;
}) {
  const [detail, setDetail] = useState<RunDetailV2 | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rerunOpen, setRerunOpen] = useState(false);
  const [assessmentOpen, setAssessmentOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("Fit");
  const { unlocked } = useUnlock();

  useEffect(() => {
    let active = true;
    setDetail(null);
    setError(null);
    api
      .runDetailV2(runId)
      .then((d) => active && setDetail(d))
      .catch((e) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [runId]);

  return (
    <div className="space-y-4">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft className="h-4 w-4" /> All runs
      </Button>

      {error && <div className="text-destructive">Error: {error}</div>}
      {!error && !detail && <div className="text-muted-foreground">Loading run…</div>}

      {detail && (
        <>
          <SummaryCard
            detail={detail}
            onOpenRun={onOpenRun}
            assessmentOpen={assessmentOpen}
            onToggleAssessment={() => setAssessmentOpen((v) => !v)}
          />

          <div className="flex flex-wrap gap-2">
            {detail.has_md && (
              <a href={api.fileUrl(runId, "cv_final.md")}>
                <Button variant="outline" size="sm">
                  <Download className="h-4 w-4" /> cv_final.md
                </Button>
              </a>
            )}
            <a href={api.runHtmlUrl(runId)} target="_blank" rel="noreferrer">
              <Button variant="outline" size="sm">
                <ExternalLink className="h-4 w-4" /> HTML report
              </Button>
            </a>
            {unlocked && onRerun && (
              <Button variant="outline" size="sm" className="ml-auto" onClick={() => setRerunOpen(true)}>
                <RotateCcw className="h-4 w-4" /> Re-run
              </Button>
            )}
          </div>

          {rerunOpen && onRerun && (
            <RerunDialog
              runId={runId}
              roleTitle={detail.fit.role_title ?? detail.company_name ?? detail.run_id}
              onClose={() => setRerunOpen(false)}
              onStarted={(newRunId) => {
                setRerunOpen(false);
                onRerun(newRunId);
              }}
              onError={(m) => setError(m)}
            />
          )}

          {/* Six tabs, rendered from the structured API (no iframe). */}
          <div className="flex flex-wrap gap-1 border-b border-border">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={
                  "px-3 py-1.5 text-sm -mb-px border-b-2 " +
                  (tab === t
                    ? "border-primary font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground")
                }
              >
                {t}
                {t === "Changes" && detail.grounding.total > 0 && (
                  <span className="ml-1 text-amber-600 dark:text-amber-500">•</span>
                )}
              </button>
            ))}
          </div>

          <div className="min-h-[40vh]">
            {tab === "Fit" && <FitTab detail={detail} />}
            {tab === "CV" && <Markdown md={detail.cv_final_md} />}
            {tab === "Changes" && <ChangesTab runId={runId} sections={detail.sections} />}
            {tab === "Scores" && <ScoresTab detail={detail} />}
            {tab === "Reasoning" && <ReasoningTab runId={runId} />}
            {tab === "JD" && (
              <pre className="whitespace-pre-wrap rounded-lg border border-border bg-muted/30 p-4 font-sans text-sm">
                {detail.jd_raw || "No JD text stored for this run."}
              </pre>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SummaryCard({
  detail,
  onOpenRun,
  assessmentOpen,
  onToggleAssessment,
}: {
  detail: RunDetailV2;
  onOpenRun?: (runId: string) => void;
  assessmentOpen: boolean;
  onToggleAssessment: () => void;
}) {
  const card = detail.card;
  const fitScore = detail.fit.score;
  return (
    <Card>
      <CardContent className="space-y-2 pt-6 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          <div className="font-medium">{detail.fit.role_title ?? detail.run_id}</div>
          <Badge variant={FIT_BAND_VARIANT[card.fit_band] ?? "secondary"}>
            Fit: {detail.fit.outcome ?? "—"}
            {fitScore != null && ` (${(fitScore * 100).toFixed(0)}%)`}
          </Badge>
          {detail.mode && <Badge variant="outline">{detail.mode}</Badge>}
          {detail.cost_usd != null && (
            <span className="ml-auto font-medium tabular-nums">
              ${detail.cost_usd.toFixed(4)} <span className="text-muted-foreground">est.</span>
            </span>
          )}
        </div>
        {detail.rerun_of && (
          <div className="text-xs text-muted-foreground">
            Re-run of{" "}
            {onOpenRun ? (
              <button
                type="button"
                onClick={() => onOpenRun(detail.rerun_of as string)}
                className="font-medium text-foreground underline-offset-2 hover:underline"
              >
                {detail.rerun_of}
              </button>
            ) : (
              <span className="font-medium text-foreground">{detail.rerun_of}</span>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground">
          <span>
            ✓ Grounded coverage:{" "}
            <span className="font-medium text-foreground tabular-nums">
              {card.grounded_pct != null ? `${card.grounded_pct}%` : "—"}
            </span>
          </span>
          <span className={card.unsupported ? "text-destructive" : undefined}>
            {card.unsupported ? "⚠" : "✓"} Unsupported claims:{" "}
            <span className="font-medium tabular-nums">{card.unsupported}</span>
          </span>
          {card.status && (
            <span>
              Status: <span className="font-medium text-foreground">{card.status}</span>
            </span>
          )}
          <span>· {detail.scores.iterations.length} iteration(s)</span>
        </div>
        {detail.job_radar_source && (
          <div className="text-xs text-muted-foreground">
            From Job Radar:{" "}
            <span className="text-foreground">{detail.job_radar_source.company ?? "job"}</span>
            {detail.job_radar_source.fit_label && ` — ${detail.job_radar_source.fit_label}`}
            {detail.job_radar_source.fit_score != null && ` (${detail.job_radar_source.fit_score})`}
            {detail.job_radar_source.source_url && (
              <a
                href={detail.job_radar_source.source_url}
                target="_blank"
                rel="noreferrer"
                className="ml-1 inline-flex items-center gap-0.5 underline-offset-2 hover:underline"
              >
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        )}
        {detail.job_radar_assessment && (
          <JobRadarAssessmentPanel
            assessment={detail.job_radar_assessment}
            open={assessmentOpen}
            onToggle={onToggleAssessment}
          />
        )}
      </CardContent>
    </Card>
  );
}

// -- Fit tab: why-a-fit narrative, transferable strengths, gaps, grounding flags ----- //
function FitTab({ detail }: { detail: RunDetailV2 }) {
  const { value_alignment, skills_transferable, gaps, no_fit_reason } = detail.fit;
  const claims = detail.grounding.claims;
  return (
    <div className="space-y-5 text-sm">
      {value_alignment && (
        <section>
          <h3 className="mb-1 font-medium">Why you’re a fit</h3>
          <p className="whitespace-pre-wrap text-muted-foreground">{value_alignment}</p>
        </section>
      )}
      {no_fit_reason && (
        <section>
          <h3 className="mb-1 font-medium text-destructive">Why this isn’t a fit</h3>
          <p className="whitespace-pre-wrap text-muted-foreground">{no_fit_reason}</p>
        </section>
      )}
      {skills_transferable.length > 0 && (
        <section>
          <h3 className="mb-1 font-medium">Transferable strengths</h3>
          <div className="flex flex-wrap gap-1.5">
            {skills_transferable.map((s, i) => (
              <Badge key={i} variant="secondary">
                {s}
              </Badge>
            ))}
          </div>
        </section>
      )}
      {gaps.length > 0 && (
        <section>
          <h3 className="mb-1 font-medium">Gaps</h3>
          <ul className="space-y-1">
            {gaps.map((g, i) => (
              <li key={i} className="text-muted-foreground">
                <span className="font-medium text-foreground">{g.requirement}</span>{" "}
                <Badge variant={g.severity === "blocking" ? "destructive" : "outline"}>
                  {g.severity}
                </Badge>{" "}
                <span className="text-xs">
                  ({g.gap_type}
                  {g.addressable ? ", addressable" : ""}) — {g.reason}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h3 className="mb-1 font-medium">
          Grounding {claims.length > 0 && <span className="text-amber-600 dark:text-amber-500">⚠</span>}
        </h3>
        {claims.length === 0 ? (
          <p className="text-muted-foreground">
            ✓ Every tailored claim traces to your source CV — no unsupported claims flagged.
          </p>
        ) : (
          <ul className="space-y-1">
            {claims.map((c, i) => (
              <li key={i} className="text-muted-foreground">
                <span className="font-medium text-foreground">{c.section}</span>: {c.issue}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

// -- Changes tab: v0→final word diff per section (lazy-loaded) ------------------------ //
function ChangesTab({ runId, sections }: { runId: string; sections: RunSection[] }) {
  const [diffs, setDiffs] = useState<Record<string, SectionDiff | null>>({});
  const [error, setError] = useState<string | null>(null);
  // Non-static sections in position order (static text is copied verbatim, D-13).
  const ids = useMemo(
    () => sections.filter((s) => !s.static).sort((a, b) => a.position - b.position).map((s) => s.section_id),
    [sections],
  );

  useEffect(() => {
    let active = true;
    setDiffs({});
    setError(null);
    Promise.all(
      ids.map((sid) =>
        api
          .sectionDiff(runId, sid)
          .then((d) => [sid, d] as const)
          .catch(() => [sid, null] as const),
      ),
    ).then((pairs) => {
      if (active) setDiffs(Object.fromEntries(pairs));
    }, (e) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [runId, ids]);

  if (error) return <div className="text-destructive">Error: {error}</div>;
  if (ids.length === 0) return <div className="text-muted-foreground">No tailored sections.</div>;

  return (
    <div className="space-y-5 text-sm">
      {ids.map((sid) => {
        const d = diffs[sid];
        return (
          <section key={sid}>
            <h3 className="mb-1 flex items-center gap-2 font-medium">
              {d?.title ?? sid}
              {d && <span className="text-xs font-normal text-muted-foreground">{d.versions.join(" → ")}</span>}
            </h3>
            {d === undefined ? (
              <div className="text-muted-foreground">Loading diff…</div>
            ) : d === null ? (
              <div className="text-muted-foreground">No diff available.</div>
            ) : (
              <div
                className="leading-relaxed [&_del]:bg-red-100 [&_del]:text-red-800 [&_del]:line-through dark:[&_del]:bg-red-950/50 dark:[&_del]:text-red-300 [&_ins]:bg-green-100 [&_ins]:text-green-800 [&_ins]:no-underline dark:[&_ins]:bg-green-950/50 dark:[&_ins]:text-green-300"
                dangerouslySetInnerHTML={{ __html: d.diff_html }}
              />
            )}
          </section>
        );
      })}
    </div>
  );
}

// -- Scores tab: per-iteration aggregate + per-section final scores ------------------- //
function ScoresTab({ detail }: { detail: RunDetailV2 }) {
  const its = detail.scores.iterations;
  const sections = detail.sections.filter((s) => !s.static);
  const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
  const num = (v: number | null) => (v == null ? "—" : v.toFixed(1));
  return (
    <div className="space-y-6 overflow-x-auto text-sm">
      <table className="w-full min-w-[32rem] border-collapse">
        <caption className="mb-1 text-left font-medium">Per iteration</caption>
        <thead className="text-muted-foreground">
          <tr className="border-b border-border text-left">
            <th className="py-1 pr-4 font-normal">#</th>
            <th className="py-1 pr-4 font-normal">Coverage</th>
            <th className="py-1 pr-4 font-normal">Quality</th>
            <th className="py-1 pr-4 font-normal">Converged</th>
            <th className="py-1 pr-4 font-normal">Active</th>
          </tr>
        </thead>
        <tbody>
          {its.map((it) => (
            <tr key={it.iteration} className="border-b border-border/50">
              <td className="py-1 pr-4 tabular-nums">{it.iteration}</td>
              <td className="py-1 pr-4 tabular-nums">{pct(it.keyword_coverage)}</td>
              <td className="py-1 pr-4 tabular-nums">{num(it.quality_score)}</td>
              <td className="py-1 pr-4 tabular-nums">{it.sections_converged ?? "—"}</td>
              <td className="py-1 pr-4 tabular-nums">{it.sections_active ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="w-full min-w-[32rem] border-collapse">
        <caption className="mb-1 text-left font-medium">Per section (final)</caption>
        <thead className="text-muted-foreground">
          <tr className="border-b border-border text-left">
            <th className="py-1 pr-4 font-normal">Section</th>
            <th className="py-1 pr-4 font-normal">Coverage</th>
            <th className="py-1 pr-4 font-normal">Claude</th>
            <th className="py-1 pr-4 font-normal">GPT</th>
            <th className="py-1 pr-4 font-normal">Selected</th>
            <th className="py-1 pr-4 font-normal">Converged</th>
          </tr>
        </thead>
        <tbody>
          {sections.map((s) => (
            <tr key={s.section_id} className="border-b border-border/50">
              <td className="py-1 pr-4">{s.section_id}</td>
              <td className="py-1 pr-4 tabular-nums">{pct(s.keyword_coverage)}</td>
              <td className="py-1 pr-4 tabular-nums">{num(s.claude_quality)}</td>
              <td className="py-1 pr-4 tabular-nums">{num(s.gpt_quality)}</td>
              <td className="py-1 pr-4">{s.selected_writer ?? "—"}</td>
              <td className="py-1 pr-4">{s.converged ? "✓" : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// -- Reasoning tab: the audit trail, grouped by phase -------------------------------- //
function ReasoningTab({ runId }: { runId: string }) {
  const [entries, setEntries] = useState<ReasoningEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setEntries(null);
    api
      .runReasoning(runId)
      .then((r) => active && setEntries(r.entries))
      .catch((e) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [runId]);

  if (error) return <div className="text-destructive">Error: {error}</div>;
  if (!entries) return <div className="text-muted-foreground">Loading reasoning…</div>;
  if (entries.length === 0) return <div className="text-muted-foreground">No reasoning recorded.</div>;

  // Group consecutive entries by phase (preserves order).
  const groups: Array<{ phase: string; items: ReasoningEntry[] }> = [];
  for (const e of entries) {
    const last = groups[groups.length - 1];
    if (last && last.phase === e.phase) last.items.push(e);
    else groups.push({ phase: e.phase, items: [e] });
  }

  return (
    <div className="space-y-4 text-sm">
      {groups.map((g, i) => (
        <section key={i}>
          <h3 className="mb-1 font-mono text-xs uppercase tracking-wide text-muted-foreground">{g.phase}</h3>
          <ul className="space-y-1">
            {g.items.map((e, j) => (
              <li key={j}>
                <span className="font-medium">{e.event}</span>
                {e.iteration != null && <span className="text-muted-foreground"> · iter {e.iteration}</span>}
                {e.reasoning && <span className="text-muted-foreground"> — {e.reasoning}</span>}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

// -- minimal markdown renderer (headings / bullets / bold) — the CV is simple md ----- //
function Markdown({ md }: { md: string | null }) {
  if (!md) return <div className="text-muted-foreground">No CV text for this run.</div>;
  const bold = (t: string) =>
    t.split(/(\*\*[^*]+\*\*)/g).map((seg, i) =>
      seg.startsWith("**") && seg.endsWith("**") ? <strong key={i}>{seg.slice(2, -2)}</strong> : seg,
    );
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      blocks.push(
        <ul key={blocks.length} className="ml-5 list-disc space-y-0.5">
          {list.map((li, i) => (
            <li key={i}>{bold(li)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  for (const raw of md.split("\n")) {
    const line = raw.trimEnd();
    if (line.startsWith("## ")) {
      flush();
      blocks.push(
        <h2 key={blocks.length} className="mt-4 border-b border-border pb-1 text-base font-semibold">
          {bold(line.slice(3))}
        </h2>,
      );
    } else if (/^\s*[-*]\s+/.test(line)) {
      list.push(line.replace(/^\s*[-*]\s+/, ""));
    } else if (!line.trim()) {
      flush();
    } else {
      flush();
      blocks.push(
        <p key={blocks.length} className="text-sm leading-relaxed">
          {bold(line)}
        </p>,
      );
    }
  }
  flush();
  return <div className="space-y-1.5">{blocks}</div>;
}

/** Re-run mode picker (SPEC_RERUN §4.1): a small modal — JD label + Demo/Full radio. */
function RerunDialog({
  runId,
  roleTitle,
  onClose,
  onStarted,
  onError,
}: {
  runId: string;
  roleTitle: string;
  onClose: () => void;
  onStarted: (newRunId: string) => void;
  onError: (message: string) => void;
}) {
  const [mode, setMode] = useState<"demo" | "full">("full");
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    try {
      const { run_id } = await api.rerun(runId, mode);
      onStarted(run_id);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Re-run this tailoring"
      description="Starts a fresh run with the same job description and Job Radar link."
      className="max-w-md"
    >
      <div className="space-y-4 text-sm">
        <div>
          <span className="text-muted-foreground">JD: </span>
          <span className="font-medium">{roleTitle}</span>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-muted-foreground">Mode:</span>
          {(["demo", "full"] as const).map((m) => (
            <label key={m} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="rerun-mode"
                value={m}
                checked={mode === m}
                onChange={() => setMode(m)}
                className="h-4 w-4"
              />
              {m === "demo" ? "Demo" : "Full"}
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void start()}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />} Start Re-run
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
