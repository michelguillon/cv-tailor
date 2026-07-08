import { useEffect, useState } from "react";
import { CorpusPage } from "@/pages/CorpusPage";
import { RunPage } from "@/pages/RunPage";
import { RunsPage } from "@/pages/RunsPage";
import { UnlockProvider } from "@/components/UnlockProvider";
import { cn } from "@/lib/utils";

// Run is Mode 1 / the default landing tab (SPEC §12.1, D-35): the primary use case is
// starting a tailoring run. Runs (history) follows; Corpus management is secondary.
const TABS = [
  { id: "run", label: "Tailor a CV" },
  { id: "runs", label: "Runs" },
  { id: "corpus", label: "Corpus" },
] as const;

export default function App() {
  const [tab, setTab] = useState<string>("run");
  // Re-run handoff (SPEC_RERUN §4.1): the Runs page POSTs the re-run, then asks us to jump to
  // the Tailor tab and attach the progress view to the already-started new run's SSE stream.
  const [attachRunId, setAttachRunId] = useState<string | null>(null);
  // "Open report" handoff: after a run finishes, the Tailor tab asks us to open its report in the
  // Runs tab's in-app OutputPanel — the SAME view as clicking "Open" in the list (no HTML download).
  const [viewRunId, setViewRunId] = useState<string | null>(null);

  function openRunStream(runId: string) {
    setAttachRunId(runId);
    setTab("run");
  }

  function openRunView(runId: string) {
    setViewRunId(runId);
    setTab("runs");
  }

  // Deep-link from the standalone report's Re-run button (SPEC_RERUN §12.11): it POSTs the re-run,
  // then bounces here with ?attach=<new_run_id>. Read it once on mount, strip it (replaceState, so a
  // refresh doesn't re-trigger), and attach the Tailor tab's progress view to that live run's stream.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const attach = params.get("attach");
    if (!attach) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("attach");
    window.history.replaceState({}, "", url.toString());
    openRunStream(attach);
  }, []);

  return (
    <UnlockProvider>
    <div className="min-h-screen">
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-5xl items-center gap-8 px-6 py-4">
          <div className="flex items-baseline gap-2">
            <h1 className="text-lg font-semibold">cv-tailor</h1>
            <span className="text-xs text-muted-foreground">multi-model CV tailoring</span>
          </div>
          <nav className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm transition-colors",
                  tab === t.id
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/60",
                )}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8">
        {tab === "corpus" && <CorpusPage />}
        {tab === "run" && (
          <RunPage
            attachRunId={attachRunId}
            onAttached={() => setAttachRunId(null)}
            onOpenReport={openRunView}
          />
        )}
        {tab === "runs" && (
          <RunsPage
            onRerun={openRunStream}
            openRunId={viewRunId}
            onOpened={() => setViewRunId(null)}
          />
        )}
      </main>
    </div>
    </UnlockProvider>
  );
}
