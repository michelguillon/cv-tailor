import { useEffect, useState } from "react";
import { ArrowLeft, Download, ExternalLink, RotateCcw, Loader2 } from "lucide-react";
import { api, type RunDetail } from "@/lib/api";
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

/** View one completed run: the D-34 summary card, downloads, and the full Phase-6 report
 *  (Fit / CV / Grounding / Changes / Scores / Reasoning / JD) embedded inline. Works for
 *  live and demo runs. Owner can Re-run it (SPEC_RERUN §4) and follow re-run lineage. */
export function OutputPanel({
  runId,
  onBack,
  onOpenRun,
  onRerun,
}: {
  runId: string;
  onBack: () => void;
  // Follow re-run lineage (SPEC_RERUN §4.2): open the original run this one was re-run from.
  onOpenRun?: (runId: string) => void;
  // Start a re-run (SPEC_RERUN §4.1): hands the new run id back so the app can stream it.
  onRerun?: (runId: string) => void;
}) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rerunOpen, setRerunOpen] = useState(false);
  const [assessmentOpen, setAssessmentOpen] = useState(false);   // Job Radar context (SPEC §12.12)
  const { unlocked } = useUnlock();

  useEffect(() => {
    let active = true;
    api
      .runDetail(runId)
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
          {/* Summary card (D-34): the at-a-glance "should I submit this?" header. */}
          <Card>
            <CardContent className="space-y-2 pt-6 text-sm">
              <div className="flex flex-wrap items-center gap-3">
                <div className="font-medium">{detail.role_title ?? detail.run_id}</div>
                <Badge variant={(detail.fit_band ? FIT_BAND_VARIANT[detail.fit_band] : undefined) ?? "secondary"}>
                  Fit: {detail.outcome ?? "—"}
                  {detail.fit_score != null && ` (${(detail.fit_score * 100).toFixed(0)}%)`}
                </Badge>
                {detail.mode && <Badge variant="outline">{detail.mode}</Badge>}
                {detail.cost_estimated_usd != null && (
                  <span className="ml-auto font-medium tabular-nums">
                    ${detail.cost_estimated_usd.toFixed(4)}{" "}
                    <span className="text-muted-foreground">est.</span>
                  </span>
                )}
              </div>
              {/* Re-run provenance (SPEC_RERUN §4.2): links to the original run this was re-run
                  from. Absent (null) on a fresh run → no badge. */}
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
                    {detail.grounded_coverage != null
                      ? `${(detail.grounded_coverage * 100).toFixed(0)}%`
                      : "—"}
                  </span>
                </span>
                <span className={detail.unsupported_claims ? "text-destructive" : undefined}>
                  {detail.unsupported_claims ? "⚠" : "✓"} Unsupported claims:{" "}
                  <span className="font-medium tabular-nums">{detail.unsupported_claims ?? "—"}</span>
                </span>
                {detail.status && (
                  <span>
                    Status: <span className="font-medium text-foreground">{detail.status}</span>
                  </span>
                )}
                <span>· {detail.iterations} iteration(s)</span>
              </div>
              {/* Job Radar provenance (Integration §5.2) — owner-only (null/absent for public);
                  links back to the originating role. */}
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
              {/* Job Radar assessment context (SPEC §12.12): the owner's manual review of the
                  role. Owner-only — the detail endpoint blanks it for a locked request, so its
                  mere presence is the gate. Collapsed by default (context, not primary info). */}
              {detail.job_radar_assessment && (
                <JobRadarAssessmentPanel
                  assessment={detail.job_radar_assessment}
                  open={assessmentOpen}
                  onToggle={() => setAssessmentOpen((v) => !v)}
                />
              )}
            </CardContent>
          </Card>

          <div className="flex flex-wrap gap-2">
            {detail.has_md && (
              <a href={api.fileUrl(runId, "cv_final.md")}>
                <Button variant="outline" size="sm">
                  <Download className="h-4 w-4" /> cv_final.md
                </Button>
              </a>
            )}
            {detail.has_html && (
              <a href={api.fileUrl(runId, "cv_final.html")}>
                <Button variant="outline" size="sm">
                  <Download className="h-4 w-4" /> cv_final.html
                </Button>
              </a>
            )}
            {detail.has_html && (
              <a href={api.reportUrl(runId)} target="_blank" rel="noreferrer">
                <Button variant="ghost" size="sm">
                  <ExternalLink className="h-4 w-4" /> Open report in new tab
                </Button>
              </a>
            )}
            {/* Re-run (SPEC_RERUN §4.1): owner-only — hidden unless unlocked. */}
            {unlocked && onRerun && (
              <Button variant="outline" size="sm" className="ml-auto" onClick={() => setRerunOpen(true)}>
                <RotateCcw className="h-4 w-4" /> Re-run
              </Button>
            )}
          </div>

          {rerunOpen && onRerun && (
            <RerunDialog
              runId={runId}
              roleTitle={detail.role_title ?? detail.company_name ?? detail.run_id}
              onClose={() => setRerunOpen(false)}
              onStarted={(newRunId) => {
                setRerunOpen(false);
                onRerun(newRunId);
              }}
              onError={(m) => setError(m)}
            />
          )}

          {detail.has_html ? (
            <iframe
              title={`report-${runId}`}
              src={api.reportUrl(runId)}
              className="h-[78vh] w-full rounded-lg border border-border bg-white"
            />
          ) : (
            <Card>
              <CardContent className="pt-6 text-sm text-muted-foreground">
                No HTML report for this run.
                {detail.cv_md && (
                  <pre className="mt-3 whitespace-pre-wrap font-sans text-foreground">{detail.cv_md}</pre>
                )}
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

/** Re-run mode picker (SPEC_RERUN §4.1): a small modal — JD label + Demo/Full radio (default
 *  full). On confirm, POST the re-run and hand the new run id back so the app streams it. */
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
