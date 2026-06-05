import { useState } from "react";
import { MessageSquare, Send, Check, X, Wand2, AlertTriangle, Loader2 } from "lucide-react";
import type { HitlCheckpoint, HitlDecision } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

// Inline chat-style HITL checkpoint (SPEC §12.3). The pipeline is paused; the panel
// renders the published payload and POSTs the human's decision via `onDecide`. After
// a decision the panel is disabled (`busy`) until the SSE stream delivers the next
// state (a new checkpoint, a confirmation, or the run resuming).

type Payload = Record<string, unknown>;

interface Gap {
  requirement: string;
  gap_type: string;
  severity: string;
  addressable: boolean;
}
interface MixRow {
  section_id: string;
  source_cv: string;
  coverage: number;
  static: boolean;
}
interface SectionRow {
  section_id: string;
  label: string;
  status: string;
  version?: number | null;
  converged_iter?: number | null;
}
interface Unresolved {
  index: number;
  label: string;
  issue: string;
  severity: string;
}
interface Preview {
  section_id: string;
  label: string;
  instruction: string;
}

export function HitlPanel({
  checkpoint,
  payload,
  busy,
  onDecide,
}: {
  checkpoint: HitlCheckpoint;
  payload: Payload;
  busy: boolean;
  onDecide: (d: HitlDecision) => void;
}) {
  return (
    <Card className="border-primary/40">
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-center gap-2 text-sm font-medium text-primary">
          <MessageSquare className="h-4 w-4" />
          {checkpoint === "fit_assessment" && "Fit assessment — your call"}
          {checkpoint === "section_review" && "Section review — your call"}
          {checkpoint === "formatting" && "Formatting — your call"}
          {busy && (
            <span className="ml-auto flex items-center gap-1.5 text-xs font-normal text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Sending…
            </span>
          )}
        </div>
        {checkpoint === "fit_assessment" && <FitBody payload={payload} busy={busy} onDecide={onDecide} />}
        {checkpoint === "section_review" && <ReviewBody payload={payload} busy={busy} onDecide={onDecide} />}
        {checkpoint === "formatting" && <FormattingBody payload={payload} busy={busy} onDecide={onDecide} />}
      </CardContent>
    </Card>
  );
}

function FitBody({ payload, busy, onDecide }: { payload: Payload; busy: boolean; onDecide: (d: HitlDecision) => void }) {
  const [text, setText] = useState("");
  const outcome = payload.outcome as string;
  const noFit = outcome === "no_fit";
  const mix = (payload.section_mix as MixRow[] | null) ?? [];
  const gaps = (payload.gaps as Gap[]) ?? [];
  const transferable = (payload.skills_transferable as string[]) ?? [];

  return (
    <div className="space-y-3 text-sm">
      <p>
        Fit for <span className="font-medium">{String(payload.role_title || "this role")}</span>:{" "}
        <Badge
          variant={outcome === "strong" ? "success" : "secondary"}
          className={noFit ? "bg-destructive text-destructive-foreground" : undefined}
        >
          {outcome}
        </Badge>{" "}
        <span className="text-muted-foreground">
          {Math.round((payload.fit_score as number) * 100)}% coverage
        </span>
      </p>

      {noFit ? (
        <p className="flex items-start gap-2 text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          {String(payload.no_fit_reason || "Significant gap — tailoring may not resolve it.")}
        </p>
      ) : (
        mix.length > 0 && (
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">Recommended section mix</div>
            <table className="w-full text-xs">
              <tbody>
                {mix.map((r) => (
                  <tr key={r.section_id} className="border-t border-border">
                    <td className="py-1 pr-2">{r.section_id}</td>
                    <td className="py-1 pr-2 text-muted-foreground">{r.source_cv}</td>
                    <td className="py-1 text-right tabular-nums">
                      {r.static ? "static" : `${Math.round(r.coverage * 100)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {transferable.length > 0 && (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium">Transferable:</span> {transferable.join(", ")}
        </p>
      )}
      {gaps.length > 0 && (
        <ul className="space-y-0.5 text-xs">
          {gaps.map((g, i) => (
            <li key={i} className={g.severity === "blocking" ? "text-destructive" : "text-muted-foreground"}>
              {g.severity === "blocking" ? "⛔" : "⚠"} {g.requirement} — {g.gap_type} / {g.severity} /{" "}
              {g.addressable ? "addressable" : "not addressable"}
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <Button size="sm" disabled={busy} onClick={() => onDecide({ action: noFit ? "override" : "proceed" })}>
          <Check className="h-4 w-4" /> {noFit ? "Override & proceed" : "Proceed"}
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide({ action: "stop" })}>
          <X className="h-4 w-4" /> Stop here
        </Button>
      </div>

      <FreeText
        value={text}
        onChange={setText}
        busy={busy}
        placeholder="…or tell me what you want (e.g. “let’s give it a go”)"
        onSend={() => {
          if (text.trim()) onDecide({ action: "freetext", text: text.trim() });
        }}
      />
    </div>
  );
}

function ReviewBody({ payload, busy, onDecide }: { payload: Payload; busy: boolean; onDecide: (d: HitlDecision) => void }) {
  const [text, setText] = useState("");
  const sections = (payload.sections as SectionRow[]) ?? [];
  const unresolved = (payload.unresolved as Unresolved[]) ?? [];
  const preview = payload.preview as Preview | null;

  if (preview) {
    return (
      <div className="space-y-3 text-sm">
        <p className="flex items-start gap-2">
          <Wand2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <span>
            Got it — I’ll revise <span className="font-medium">{preview.label}</span>:{" "}
            <span className="text-muted-foreground">“{preview.instruction}”</span>
          </span>
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              onDecide({ action: "apply_freetext", section_id: preview.section_id, instruction: preview.instruction })
            }
          >
            <Check className="h-4 w-4" /> Apply
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide({ action: "accept" })}>
            Cancel & accept all
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3 text-sm">
      <p className="text-xs text-muted-foreground">
        Refinement {String(payload.iterations)}/{String(payload.max_iterations)} iteration(s) —{" "}
        {String(payload.convergence_reason ?? "")}
      </p>
      <ul className="space-y-0.5 text-xs">
        {sections.map((s) => (
          <li key={s.section_id} className="flex items-center gap-2">
            <span className="text-muted-foreground">
              {s.status === "static" ? "—" : s.status === "converged" ? "✓" : "~"}
            </span>
            <span>{s.label}</span>
            <span className="text-muted-foreground">
              {s.status === "static"
                ? "static"
                : s.status === "converged"
                  ? `converged${s.converged_iter ? ` (iter ${s.converged_iter})` : ""}`
                  : "did not converge"}
            </span>
          </li>
        ))}
      </ul>

      {unresolved.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">Unresolved items</div>
          {unresolved.map((u) => (
            <div key={u.index} className="flex items-start justify-between gap-2 text-xs">
              <span>
                <span className="font-medium">{u.label}:</span> “{u.issue}”{" "}
                <span className="text-muted-foreground">({u.severity})</span>
              </span>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => onDecide({ action: "apply_item", index: u.index })}
              >
                Apply
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <Button size="sm" disabled={busy} onClick={() => onDecide({ action: "accept" })}>
          <Check className="h-4 w-4" /> Accept all & continue
        </Button>
      </div>

      <FreeText
        value={text}
        onChange={setText}
        busy={busy}
        placeholder="…or describe a change (e.g. “make the profile punchier”)"
        onSend={() => {
          if (text.trim()) {
            onDecide({ action: "interpret", text: text.trim() });
            setText("");
          }
        }}
      />
    </div>
  );
}

function FormattingBody({ payload, busy, onDecide }: { payload: Payload; busy: boolean; onDecide: (d: HitlDecision) => void }) {
  const corrections =
    (payload.corrections as Array<{ section_id: string; fixes: string[] }>) ?? [];
  const length = payload.length as { total_words?: number; budget_words?: number; over_budget?: boolean } | undefined;

  return (
    <div className="space-y-3 text-sm">
      <p>Apply these formatting corrections?</p>
      <div className="space-y-1">
        {corrections.map((c) => (
          <div key={c.section_id} className="text-xs">
            <span className="font-medium">{c.section_id}</span>
            <ul className="ml-4 list-disc text-muted-foreground">
              {c.fixes.map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {length && (
        <p className="text-xs text-muted-foreground">
          Assembled length: {length.total_words} / {length.budget_words} words{" "}
          {length.over_budget ? "⚠ over" : "ok"}
        </p>
      )}
      <div className="flex flex-wrap gap-2 pt-1">
        <Button size="sm" disabled={busy} onClick={() => onDecide({ action: "approve" })}>
          <Check className="h-4 w-4" /> Approve
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide({ action: "reject" })}>
          <X className="h-4 w-4" /> Reject
        </Button>
      </div>
    </div>
  );
}

function FreeText({
  value,
  onChange,
  onSend,
  busy,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  busy: boolean;
  placeholder: string;
}) {
  return (
    <div className="flex items-center gap-2 pt-1">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={busy}
        placeholder={placeholder}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSend();
        }}
        className="h-8 flex-1 rounded-md border border-border bg-background px-3 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      />
      <Button size="sm" variant="ghost" disabled={busy || !value.trim()} onClick={onSend}>
        <Send className="h-4 w-4" />
      </Button>
    </div>
  );
}
