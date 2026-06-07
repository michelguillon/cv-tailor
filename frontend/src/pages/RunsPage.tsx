import { useEffect, useState } from "react";
import {
  RefreshCw, FileText, Trash2, Star, Globe, Pencil, AlertTriangle, Lock, LockOpen, Loader2,
} from "lucide-react";
import { api, type ArchiveRun } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { OutputPanel } from "@/components/OutputPanel";
import { useUnlock } from "@/components/UnlockProvider";

const FIT_LABEL: Record<string, string> = { strong: "Strong", partial: "Partial", low: "No fit" };

export function RunsPage() {
  const [runs, setRuns] = useState<ArchiveRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<ArchiveRun | null>(null); // company-name edit dialog

  // Runs are capability-aware (D-40/§12.9): locked sessions get only curated public-demo
  // runs (redacted); the owner (valid cookie) gets all runs + management controls.
  const { configured, unlocked, requestUnlock, lock } = useUnlock();

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setRuns(await api.archiveRuns());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, [unlocked]); // re-fetch when unlock state flips so the list widens/narrows

  async function onUnlock() {
    if (await requestUnlock()) await load();
  }

  async function onLock() {
    await lock();
    await load();
  }

  // Owner actions — guarded by requestUnlock so an expired cookie re-prompts rather than 403s.
  async function toggleMeta(r: ArchiveRun, patch: { keep?: boolean; public_demo?: boolean }) {
    if (!(await requestUnlock())) return;
    try {
      await api.setRunMeta(r.run_id, patch);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onDelete(r: ArchiveRun) {
    if (!(await requestUnlock())) return;
    const label = r.company_name?.trim() || r.role_title || r.run_id;
    if (!window.confirm(`Delete the run for ${label}? This removes its output directory permanently.`))
      return;
    try {
      await api.deleteRun(r.run_id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onCleanup() {
    if (!(await requestUnlock())) return;
    if (!window.confirm("Delete all stale private runs (older than the retention window, except kept or public-demo runs)?"))
      return;
    try {
      const r = await api.cleanupRuns();
      await load();
      window.alert(`Removed ${r.count} stale run(s).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (selected) return <OutputPanel runId={selected} onBack={() => setSelected(null)} />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Runs</h2>
          <p className="text-sm text-muted-foreground">
            {unlocked
              ? "All tailoring runs. Open one to re-view it; mark runs to keep or publish, or delete them."
              : "Curated demo runs. Open any one to re-view its CV, changes, scores, and reasoning."}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <RunsLockStatus configured={configured} unlocked={unlocked} onLock={() => void onLock()} />
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void load()}>
              <RefreshCw className="h-4 w-4" /> Refresh
            </Button>
            {configured && !unlocked && (
              <Button size="sm" onClick={() => void onUnlock()}>
                <Lock className="h-4 w-4" /> Unlock to manage
              </Button>
            )}
            {unlocked && (
              <Button variant="outline" size="sm" onClick={() => void onCleanup()}>
                <Trash2 className="h-4 w-4" /> Clean up old runs
              </Button>
            )}
          </div>
        </div>
      </div>

      {loading && <div className="text-muted-foreground">Loading runs…</div>}
      {error && <div className="text-destructive">Error: {error}</div>}
      {!loading && !error && runs.length === 0 && (
        <div className="text-muted-foreground">
          {unlocked ? "No runs yet — tailor a CV to create one." : "No public demo runs to show."}
        </div>
      )}

      <div className="space-y-2">
        {runs.map((r) => {
          const company = r.company_name?.trim() || "Unknown company";
          const fitLabel = r.fit_band ? FIT_LABEL[r.fit_band] ?? r.fit_band : null;
          const fitPct = r.fit_score != null ? `${Math.round(r.fit_score * 100)}%` : null;
          return (
            <Card key={r.run_id}>
              <CardContent className="flex flex-wrap items-center gap-x-3 gap-y-2 py-4 text-sm">
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="font-medium">
                  {company} <span className="text-muted-foreground">— {r.role_title ?? r.run_id}</span>
                </span>
                {fitLabel && (
                  <Badge variant={r.fit_band === "strong" ? "success" : "secondary"}>
                    {fitLabel}
                    {fitPct ? ` · ${fitPct}` : ""}
                  </Badge>
                )}
                {r.mode && <Badge variant="outline">{r.mode}</Badge>}
                <span className="text-muted-foreground">{r.iterations ?? 0} iter(s)</span>

                {/* Owner-only metadata + warnings */}
                {unlocked && r.cost_estimated_usd != null && (
                  <span className="tabular-nums text-muted-foreground">
                    ${r.cost_estimated_usd.toFixed(4)}
                  </span>
                )}
                {unlocked && r.created_at && (
                  <span className="text-xs text-muted-foreground">
                    {new Date(r.created_at).toLocaleDateString()}
                  </span>
                )}
                {unlocked && (r.unsupported_claims ?? 0) > 0 && (
                  <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
                    <AlertTriangle className="h-3.5 w-3.5" /> {r.unsupported_claims} unsupported claim
                    {r.unsupported_claims === 1 ? "" : "s"}
                  </span>
                )}

                <div className="ml-auto flex items-center gap-2">
                  <Button variant="outline" size="sm" onClick={() => setSelected(r.run_id)}>
                    Open
                  </Button>
                  {unlocked && (
                    <>
                      <Button
                        variant={r.keep ? "default" : "outline"}
                        size="sm"
                        title={r.keep ? "Kept — protected from cleanup" : "Keep (protect from cleanup)"}
                        onClick={() => void toggleMeta(r, { keep: !r.keep })}
                      >
                        <Star className="h-4 w-4" /> Keep
                      </Button>
                      <Button
                        variant={r.public_demo ? "default" : "outline"}
                        size="sm"
                        title={r.public_demo ? "Visible to public visitors" : "Mark as a public demo run"}
                        onClick={() => void toggleMeta(r, { public_demo: !r.public_demo })}
                      >
                        <Globe className="h-4 w-4" /> Public
                      </Button>
                      <Button
                        variant="outline"
                        size="icon"
                        title="Edit company name"
                        onClick={() => setEditing(r)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="destructive" size="icon" title="Delete run" onClick={() => void onDelete(r)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </>
                  )}
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {editing && (
        <EditCompanyDialog
          run={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
          onError={(m) => setError(m)}
          requestUnlock={requestUnlock}
        />
      )}
    </div>
  );
}

// Mirrors the corpus page lock indicator: viewing is public, managing runs needs the owner
// unlock; on a no-key (read-only) deployment there's nothing to unlock.
function RunsLockStatus({
  configured,
  unlocked,
  onLock,
}: {
  configured: boolean;
  unlocked: boolean;
  onLock: () => void;
}) {
  if (configured && unlocked) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-success">
        <LockOpen className="h-3.5 w-3.5" /> owner unlocked
        <button
          type="button"
          onClick={onLock}
          className="ml-1 text-muted-foreground underline-offset-2 hover:underline"
        >
          lock
        </button>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Lock className="h-3.5 w-3.5" />
      {configured ? "showing public demo runs" : "showing public demo runs — read-only"}
    </span>
  );
}

function EditCompanyDialog({
  run,
  onClose,
  onSaved,
  onError,
  requestUnlock,
}: {
  run: ArchiveRun;
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
  requestUnlock: () => Promise<boolean>;
}) {
  const [value, setValue] = useState(run.company_name ?? "");
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!(await requestUnlock())) return;
    setBusy(true);
    try {
      await api.setRunMeta(run.run_id, { company_name: value.trim() });
      onSaved();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Edit company name"
      description="Shown in the run list so runs read by company and role, not run ids."
      className="max-w-md"
    >
      <div className="space-y-4">
        <input
          type="text"
          value={value}
          autoFocus
          placeholder="Company name"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
          }}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void save()}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />} Save
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
