import { useEffect, useState } from "react";
import { RefreshCw, FileText } from "lucide-react";
import { api, type ArchiveRun } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { OutputPanel } from "@/components/OutputPanel";

export function RunsPage() {
  const [runs, setRuns] = useState<ArchiveRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

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
  }, []);

  if (selected) return <OutputPanel runId={selected} onBack={() => setSelected(null)} />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Runs</h2>
          <p className="text-sm text-muted-foreground">
            Past tailoring runs. Open any one to re-view its CV, changes, scores, and reasoning —
            no re-spend.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void load()}>
          <RefreshCw className="h-4 w-4" /> Refresh
        </Button>
      </div>

      {loading && <div className="text-muted-foreground">Loading runs…</div>}
      {error && <div className="text-destructive">Error: {error}</div>}
      {!loading && !error && runs.length === 0 && (
        <div className="text-muted-foreground">No runs yet — tailor a CV to create one.</div>
      )}

      <div className="space-y-2">
        {runs.map((r) => (
          <Card key={r.run_id}>
            <CardContent className="flex flex-wrap items-center gap-3 py-4 text-sm">
              <FileText className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">{r.role_title ?? r.run_id}</span>
              {r.outcome && (
                <Badge variant={r.outcome === "strong" ? "success" : "secondary"}>{r.outcome}</Badge>
              )}
              {r.mode && <Badge variant="outline">{r.mode}</Badge>}
              <span className="text-muted-foreground">{r.iterations} iter(s)</span>
              <span className="font-mono text-xs text-muted-foreground">{r.run_id}</span>
              {r.cost_estimated_usd != null && (
                <span className="tabular-nums text-muted-foreground">
                  ${r.cost_estimated_usd.toFixed(4)}
                </span>
              )}
              <Button
                variant="outline"
                size="sm"
                className="ml-auto"
                onClick={() => setSelected(r.run_id)}
              >
                Open
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
