import { useEffect, useState } from "react";
import { ArrowLeft, Download, ExternalLink } from "lucide-react";
import { api, type RunDetail } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

const FIT_BAND_VARIANT: Record<string, "success" | "secondary" | "destructive"> = {
  strong: "success",
  partial: "secondary",
  low: "destructive",
};

/** View one completed run: the D-34 summary card, downloads, and the full Phase-6 report
 *  (Fit / CV / Grounding / Changes / Scores / Reasoning / JD) embedded inline. Works for
 *  live and demo runs. */
export function OutputPanel({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

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
          </div>

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
