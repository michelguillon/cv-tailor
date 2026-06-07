import { useEffect, useState } from "react";
import { RefreshCw, Trash2, ChevronDown, ChevronRight, Plus, Pencil, Upload, Lock, LockOpen } from "lucide-react";
import { api, type CorpusStats, type CVItem } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CvWizard } from "@/components/CvWizard";
import { EditMetadataDialog } from "@/components/EditMetadataDialog";
import { useUnlock } from "@/components/UnlockProvider";

// Which modal is open over the corpus list, if any.
type Modal =
  | { kind: "add" }
  | { kind: "replace"; cv: CVItem }
  | { kind: "edit"; cv: CVItem }
  | null;

export function CorpusPage() {
  const [stats, setStats] = useState<CorpusStats | null>(null);
  const [cvs, setCvs] = useState<CVItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [modal, setModal] = useState<Modal>(null);

  // Corpus writes are gated on the owner unlock (D-39/§12.8) — the SAME capability that
  // unlocks full mode. Reads stay public; a write control opens the shared unlock dialog
  // when locked, and is hidden entirely when no owner key is configured (read-only deploy).
  const { configured, unlocked, requestUnlock, lock } = useUnlock();

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [s, c] = await Promise.all([api.corpusStats(), api.listCVs()]);
      setStats(s);
      setCvs(c);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // Open a write flow only once the session is unlocked (requestUnlock resolves true
  // immediately if already unlocked, false if the user cancels the prompt).
  async function openAdd() {
    if (await requestUnlock()) setModal({ kind: "add" });
  }
  async function openEdit(cv: CVItem) {
    if (await requestUnlock()) setModal({ kind: "edit", cv });
  }
  async function openReplace(cv: CVItem) {
    if (await requestUnlock()) setModal({ kind: "replace", cv });
  }

  async function onDelete(filename: string, label: string) {
    if (!(await requestUnlock())) return;
    if (!window.confirm(`Remove ${label} from the corpus? This deletes its sections from ChromaDB.`))
      return;
    try {
      await api.deleteCV(filename);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (loading) return <div className="text-muted-foreground">Loading corpus…</div>;
  if (error) return <div className="text-destructive">Error: {error}</div>;

  const filenames = cvs.map((c) => c.filename);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Corpus</h2>
          <p className="text-sm text-muted-foreground">
            The ingested CV versions tailoring draws from. Sections are the unit of retrieval.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <LockStatus configured={configured} unlocked={unlocked} onLock={() => void lock()} />
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void load()}>
              <RefreshCw className="h-4 w-4" /> Refresh
            </Button>
            {/* Write controls appear only on a deployment with an owner key (D-39); selecting
                one opens the shared unlock dialog unless this session is already unlocked. */}
            {configured && (
              <Button size="sm" onClick={() => void openAdd()}>
                <Plus className="h-4 w-4" /> Add CV
              </Button>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <Stat label="CV versions" value={stats?.cv_count ?? 0} />
        <Stat label="Sections" value={stats?.section_count ?? 0} />
        <Stat label="Last ingested" value={stats?.last_ingested ?? "—"} />
      </div>

      {cvs.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center text-sm text-muted-foreground">
            <p>No CVs in the corpus yet. Add one to get started.</p>
            {configured && (
              <Button onClick={() => void openAdd()}>
                <Plus className="h-4 w-4" /> Add CV
              </Button>
            )}
            <div className="text-xs">
              Or seed from the host:
              <pre className="mt-2 inline-block rounded-md bg-muted px-3 py-2 text-left">
                docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
              </pre>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="space-y-3">
        {cvs.map((cv) => {
          const expanded = open === cv.filename;
          return (
            <Card key={cv.filename}>
              <CardHeader className="flex-row items-center justify-between py-4">
                <div className="min-w-0">
                  <CardTitle className="truncate text-base">{cv.display_name}</CardTitle>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                    <Badge variant={cv.cv_type === "generic" ? "secondary" : "default"}>
                      {cv.cv_type}
                    </Badge>
                    <span>{cv.target_role}</span>
                    <span>·</span>
                    <span>{cv.seniority}</span>
                    <span>·</span>
                    <span>{cv.section_count} sections</span>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setOpen(expanded ? null : cv.filename)}
                  >
                    {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    Sections
                  </Button>
                  {/* Write actions are shown only on an owner-keyed deployment (D-39); each
                      opens the unlock dialog when locked, then proceeds. */}
                  {configured && (
                    <>
                      <Button variant="outline" size="sm" onClick={() => void openEdit(cv)}>
                        <Pencil className="h-4 w-4" /> Edit
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => void openReplace(cv)}>
                        <Upload className="h-4 w-4" /> Replace
                      </Button>
                      <Button variant="destructive" size="icon" onClick={() => void onDelete(cv.filename, cv.display_name)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </>
                  )}
                </div>
              </CardHeader>
              {expanded && (
                <CardContent>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-muted-foreground">
                        <th className="pb-1 font-medium">Section</th>
                        <th className="pb-1 font-medium">Type</th>
                        <th className="pb-1 font-medium">Words</th>
                        <th className="pb-1 font-medium"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {cv.sections.map((s) => (
                        <tr key={s.section_id} className="border-t border-border">
                          <td className="py-1.5 font-mono text-xs">{s.section_id}</td>
                          <td className="py-1.5">{s.section_type}</td>
                          <td className="py-1.5 tabular-nums">{s.word_count}</td>
                          <td className="py-1.5">
                            {s.static && <Badge variant="outline">static</Badge>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </CardContent>
              )}
            </Card>
          );
        })}
      </div>

      {modal?.kind === "add" && (
        <CvWizard
          mode="add"
          existingFilenames={filenames}
          onClose={() => setModal(null)}
          onDone={() => void load()}
        />
      )}
      {modal?.kind === "replace" && (
        <CvWizard
          mode="replace"
          seed={modal.cv}
          existingFilenames={filenames}
          onClose={() => setModal(null)}
          onDone={() => void load()}
        />
      )}
      {modal?.kind === "edit" && (
        <EditMetadataDialog cv={modal.cv} onClose={() => setModal(null)} onDone={() => void load()} />
      )}
    </div>
  );
}

// Makes clear that viewing is public but editing requires owner unlock (§12.8):
// unlocked → enabled + a lock affordance; locked → "unlock to edit"; no key → read-only.
function LockStatus({
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
      {configured ? "viewing is public — unlock to edit" : "viewing is public — read-only deployment"}
    </span>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        <div className="text-sm text-muted-foreground">{label}</div>
      </CardContent>
    </Card>
  );
}
