import { useState } from "react";
import { CorpusPage } from "@/pages/CorpusPage";
import { cn } from "@/lib/utils";

const TABS = [
  { id: "corpus", label: "Corpus" },
  { id: "run", label: "Tailor a CV" },
] as const;

export default function App() {
  const [tab, setTab] = useState<string>("corpus");

  return (
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
          <div className="text-muted-foreground">
            The tailoring-run UI (paste a JD → watch it run → download) lands in UI Step 3.
          </div>
        )}
      </main>
    </div>
  );
}
