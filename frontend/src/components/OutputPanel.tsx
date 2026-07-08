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

const TABS = ["Fit", "CV", "Grounding", "Changes", "Scores", "Reasoning", "JD"] as const;
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
                {t === "Grounding" && detail.grounding.total > 0 && (
                  <span className="ml-1 text-amber-600 dark:text-amber-500">•</span>
                )}
              </button>
            ))}
          </div>

          <div className="min-h-[40vh]">
            {tab === "Fit" && <FitTab detail={detail} />}
            {tab === "CV" && (
              <div className="rounded-lg border border-border bg-card p-5">
                <Markdown md={detail.cv_final_md} />
              </div>
            )}
            {tab === "Grounding" && <GroundingTab grounding={detail.grounding} />}
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

// -- Fit tab: role-fit header + bar, two color-coded columns, grounding (mirrors report) - //
const BAR_BG: Record<string, string> = {
  strong: "bg-emerald-500",
  partial: "bg-amber-500",
  low: "bg-red-500",
};

function FitTab({ detail }: { detail: RunDetailV2 }) {
  const { outcome, value_alignment, skills_transferable, gaps, no_fit_reason } = detail.fit;
  const pct = detail.card.fit_pct;
  return (
    <div className="space-y-4 text-sm">
      {/* Role fit — accent bar + blue title + value-alignment narrative. */}
      <div className="rounded-lg border border-l-4 border-border border-l-sky-500 bg-muted/20 p-4">
        <h3 className="font-semibold text-sky-600 dark:text-sky-400">
          Role fit — {outcome ?? "—"}
          {pct != null && ` (${pct}%)`}
        </h3>
        {pct != null && (
          <div className="my-2.5 h-1.5 w-full overflow-hidden rounded-full bg-border">
            <div className={`h-full rounded-full ${BAR_BG[detail.card.fit_band] ?? "bg-sky-500"}`} style={{ width: `${pct}%` }} />
          </div>
        )}
        {value_alignment ? (
          <p className="whitespace-pre-wrap leading-relaxed text-muted-foreground">{value_alignment}</p>
        ) : no_fit_reason ? (
          <p className="whitespace-pre-wrap leading-relaxed text-red-600 dark:text-red-400">{no_fit_reason}</p>
        ) : (
          <p className="italic text-muted-foreground">
            No value-alignment summary for this run (add a candidate value model to enable it).
          </p>
        )}
      </div>

      {/* Two columns: strong alignment (green) · potential gaps (red, bold majors). */}
      <div className="grid gap-4 sm:grid-cols-2">
        {skills_transferable.length > 0 && (
          <div className="rounded-lg border border-border p-4">
            <h3 className="mb-2 font-semibold text-emerald-600 dark:text-emerald-400">Strong alignment</h3>
            <ul className="ml-4 list-disc space-y-1 text-muted-foreground marker:text-emerald-500">
              {skills_transferable.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          </div>
        )}
        {gaps.length > 0 && (
          <div className="rounded-lg border border-border p-4">
            <h3 className="mb-2 font-semibold text-red-600 dark:text-red-400">Potential gaps</h3>
            <ul className="ml-4 list-disc space-y-1 marker:text-red-500">
              {gaps.map((g, i) => {
                const major = g.severity === "major" || g.severity === "blocking";
                return (
                  <li key={i} className={major ? "font-semibold text-red-600 dark:text-red-400" : "text-muted-foreground"}>
                    {g.requirement}{" "}
                    <span className="text-xs font-normal text-muted-foreground">
                      — {g.gap_type} / {g.severity} / {g.addressable ? "addressable" : "not addressable"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

    </div>
  );
}

// -- Grounding tab: the verifier's unsupported-claim flags (F-35), red-headed like the report - //
function GroundingTab({ grounding }: { grounding: RunDetailV2["grounding"] }) {
  if (grounding.total === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm">
        <h3 className="font-semibold text-emerald-600 dark:text-emerald-400">
          ✓ Every tailored section traces to your source CV.
        </h3>
        <p className="mt-1 text-muted-foreground">
          The verifier found no claim in the final CV that your source corpus doesn’t support.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <div className="rounded-lg border border-red-300 bg-red-50 p-4 dark:border-red-900/60 dark:bg-red-950/30">
        <h3 className="font-semibold text-red-600 dark:text-red-400">
          ⚠ {grounding.total} unsupported claim{grounding.total === 1 ? "" : "s"} across{" "}
          {grounding.sections} section{grounding.sections === 1 ? "" : "s"} — review before sending.
        </h3>
        <p className="mt-1 text-muted-foreground">
          These appear in the tailored CV but were not found in your source corpus. They were raised
          at the review step.
        </p>
      </div>
      {grounding.claims.map((c, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-4">
          <h3 className="font-semibold text-red-600 dark:text-red-400">{c.section}</h3>
          <p className="mt-1">{c.issue}</p>
          {c.suggestion && <p className="mt-1 text-xs text-muted-foreground">{c.suggestion}</p>}
        </div>
      ))}
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

// -- Scores tab: per-section final scores + per-iteration progression (bordered, labelled) - //
function ScoresTab({ detail }: { detail: RunDetailV2 }) {
  const its = detail.scores.iterations;
  const sections = detail.sections.filter((s) => !s.static);
  const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
  const num = (v: number | null) => (v == null ? "—" : v.toFixed(1));
  const delta = (v: number | null) => (v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(3));
  const th = "border border-border bg-muted/60 px-2 py-1.5 text-left font-medium";
  const td = "border border-border px-2 py-1.5";
  const tdn = td + " text-right tabular-nums";

  return (
    <div className="space-y-6 overflow-x-auto text-[13px]">
      <div>
        <div className="mb-1 font-medium">Per section (final)</div>
        <table className="w-full min-w-[34rem] border-collapse">
          <thead>
            <tr>
              <th className={th}>Section</th>
              <th className={th + " text-right"}>Coverage</th>
              <th className={th}>Quality (selected)</th>
              <th className={th + " text-right"}>Claude</th>
              <th className={th + " text-right"}>GPT</th>
              <th className={th}>State</th>
            </tr>
          </thead>
          <tbody>
            {sections.map((s) => (
              <tr key={s.section_id}>
                <td className={td}>{s.section_id}</td>
                <td className={tdn}>{pct(s.keyword_coverage)}</td>
                <td className={td}>
                  {s.selected_writer && (
                    <span className="rounded-full bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-700 dark:bg-sky-950/60 dark:text-sky-300">
                      {s.selected_writer}
                    </span>
                  )}
                </td>
                <td className={tdn}>{num(s.claude_quality)}</td>
                <td className={tdn}>{num(s.gpt_quality)}</td>
                <td className={td}>
                  {s.converged ? (
                    <span className="font-medium text-emerald-600 dark:text-emerald-400">✓ frozen</span>
                  ) : (
                    <span className="text-muted-foreground">active</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <div className="mb-1 font-medium">Per iteration (progression)</div>
        <table className="w-full min-w-[34rem] border-collapse">
          <thead>
            <tr>
              <th className={th + " text-right"}>Iter</th>
              <th className={th + " text-right"}>Coverage</th>
              <th className={th + " text-right"}>Quality</th>
              <th className={th + " text-right"}>Δ coverage</th>
              <th className={th + " text-right"}>Δ quality</th>
              <th className={th + " text-right"}>Converged</th>
              <th className={th + " text-right"}>Active</th>
            </tr>
          </thead>
          <tbody>
            {its.map((it) => (
              <tr key={it.iteration}>
                <td className={tdn}>{it.iteration}</td>
                <td className={tdn}>{pct(it.keyword_coverage)}</td>
                <td className={tdn}>{num(it.quality_score)}</td>
                <td className={tdn + " text-muted-foreground"}>{delta(it.keyword_delta)}</td>
                <td className={tdn + " text-muted-foreground"}>{delta(it.quality_delta)}</td>
                <td className={tdn}>{it.sections_converged ?? "—"}</td>
                <td className={tdn}>{it.sections_active ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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
        <h2
          key={blocks.length}
          className="mb-2 mt-5 border-b border-border pb-1 text-sm font-semibold uppercase tracking-wide text-sky-600 first:mt-0 dark:text-sky-400"
        >
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
