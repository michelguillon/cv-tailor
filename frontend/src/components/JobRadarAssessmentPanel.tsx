import { type ReactNode } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import { type JobRadarAssessment } from "@/lib/api";

// The owner's Job Radar assessment of a role (SPEC §12.12) — a collapsible context panel shared
// by the Run page (launch-time, from the prefill) and the run detail panel (from the run detail
// endpoint, owner-only). Renders only the fields that are present; the scorer label and the
// owner's override are shown together (`strong_fit → good_fit`) when an override exists.
// Owner-only — the caller gates visibility (the backend also blanks it for a locked request).
export function JobRadarAssessmentPanel({
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
