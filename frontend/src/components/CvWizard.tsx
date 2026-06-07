import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FileUp, Loader2 } from "lucide-react";
import { api, ApiError, type CVItem, type CvMetadataFields, type UploadPreview } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { CvMetadataForm, blankMetadata, validateMetadata } from "@/components/CvMetadataForm";

type Step = "upload" | "form" | "inventory" | "done";

// Add CV (mode "add") and Replace .docx (mode "replace") share the 4-step flow:
// upload → metadata form → section-inventory gate (R-01/D-36) → confirm. Replace
// pre-fills the form from the existing CV and stores under its filename (de-dup key).
export function CvWizard({
  mode,
  seed,
  existingFilenames,
  onClose,
  onDone,
}: {
  mode: "add" | "replace";
  seed?: CVItem;
  existingFilenames: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [step, setStep] = useState<Step>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState<CvMetadataFields>(blankMetadata());
  const [touched, setTouched] = useState<Set<keyof CvMetadataFields>>(new Set());
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [committed, setCommitted] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Replace stores the new .docx under the existing filename so it de-dups (D-10).
  const effectiveName = mode === "replace" && seed ? seed.filename : file?.name ?? "";
  const errors = validateMetadata(metadata);
  const shownErrors = Object.fromEntries(
    Object.entries(errors).filter(([k]) => touched.has(k as keyof CvMetadataFields)),
  );
  const titles = {
    add: "Add CV",
    replace: `Replace ${seed?.display_name ?? "CV"}`,
  };

  // Replace: pre-fill the form from the existing CV's metadata.
  useEffect(() => {
    if (mode === "replace" && seed) {
      api
        .cvMetadata(seed.filename)
        .then((m) => setMetadata({ ...m, filename: seed.filename }))
        .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    }
  }, [mode, seed]);

  function onPickFile(f: File | null) {
    setError(null);
    setFile(f);
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".docx")) {
      setError("Only .docx files are supported.");
      return;
    }
    const name = mode === "replace" && seed ? seed.filename : f.name;
    if (mode === "add" && existingFilenames.includes(name)) {
      setError("This CV is already in the corpus. Use Replace to update it.");
      return;
    }
    setMetadata((m) => ({ ...m, filename: name }));
  }

  async function onPreview() {
    setTouched(new Set(Object.keys(metadata) as (keyof CvMetadataFields)[]));
    if (Object.keys(errors).length > 0 || !file) return;
    setBusy(true);
    setError(null);
    try {
      // Replace: send the chosen .docx under the existing filename (the de-dup key).
      const toSend =
        mode === "replace" && seed ? new File([file], seed.filename, { type: file.type }) : file;
      const p = await api.uploadCV(toSend, { ...metadata, filename: effectiveName }, mode === "replace");
      setPreview(p);
      setStep("inventory");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(e.message);
        setStep("upload");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function onConfirm() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.confirmCV({
        token: preview.token,
        filename: preview.filename,
        metadata: { ...metadata, filename: preview.filename },
        replace: mode === "replace",
      });
      setCommitted(r.sections_committed);
      setStep("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={onClose} title={titles[mode]} className="max-w-xl">
      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {step === "upload" && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {mode === "replace"
              ? "Upload the new .docx. It replaces the existing version's sections in ChromaDB."
              : "Upload a CV .docx to add to the corpus."}
          </p>
          <label className="flex cursor-pointer flex-col items-center gap-2 rounded-md border border-dashed border-border bg-muted/40 px-6 py-10 text-center text-sm hover:bg-muted">
            <FileUp className="h-6 w-6 text-muted-foreground" />
            {file ? (
              <span className="font-medium">{effectiveName}</span>
            ) : (
              <span className="text-muted-foreground">Click to choose a .docx file</span>
            )}
            <input
              type="file"
              accept=".docx"
              className="hidden"
              onChange={(e) => onPickFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button disabled={!file || !!error} onClick={() => setStep("form")}>
              Next
            </Button>
          </div>
        </div>
      )}

      {step === "form" && (
        <div className="space-y-4">
          <CvMetadataForm
            value={metadata}
            onChange={setMetadata}
            errors={shownErrors}
            onBlurField={(f) => setTouched((t) => new Set(t).add(f))}
          />
          <div className="flex justify-between gap-2">
            <Button variant="outline" onClick={() => setStep("upload")}>
              Back
            </Button>
            <Button disabled={busy} onClick={() => void onPreview()}>
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} Preview sections
            </Button>
          </div>
        </div>
      )}

      {step === "inventory" && preview && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h4 className="font-medium">Sections to be added ({preview.section_count})</h4>
            {mode === "replace" && seed && (
              <span className="text-sm text-muted-foreground">
                Replacing {seed.section_count} existing with {preview.section_count} new
              </span>
            )}
          </div>

          {preview.below_minimum && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Only {preview.section_count} sections parsed (expected at least {preview.min_sections}).
                This usually means a parse failure — check the .docx before confirming.
              </span>
            </div>
          )}

          <div className="max-h-72 overflow-y-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <tbody>
                {preview.sections.map((s) => (
                  <tr key={s.section_id} className="border-b border-border last:border-0">
                    <td className="px-3 py-1.5 font-mono text-xs">{s.section_id}</td>
                    <td className="px-3 py-1.5 tabular-nums text-muted-foreground">~{s.word_count} words</td>
                    <td className="px-3 py-1.5">{s.static && <Badge variant="outline">static</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.warnings.map((w) => (
            <p key={w} className="text-xs text-amber-600 dark:text-amber-500">
              ⚠ {w}
            </p>
          ))}

          <div className="flex justify-between gap-2">
            <Button variant="outline" onClick={() => setStep("form")}>
              Back
            </Button>
            <Button disabled={busy} onClick={() => void onConfirm()}>
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              {mode === "replace" ? "Confirm & Replace" : "Confirm & Add to Corpus"}
            </Button>
          </div>
        </div>
      )}

      {step === "done" && (
        <div className="space-y-5 text-center">
          <CheckCircle2 className="mx-auto h-10 w-10 text-emerald-500" />
          <p className="text-sm">
            ✓ CV {mode === "replace" ? "replaced" : "added"}. {committed} sections committed.
          </p>
          <Button
            onClick={() => {
              onDone();
              onClose();
            }}
          >
            Close
          </Button>
        </div>
      )}
    </Dialog>
  );
}
