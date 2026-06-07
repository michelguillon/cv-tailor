import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { api, type CVItem, type CvMetadataFields } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { CvMetadataForm, blankMetadata, validateMetadata } from "@/components/CvMetadataForm";

// Edit Metadata (D-36): one step, no re-ingestion. Pre-fills from the existing
// sidecar/metadata, saves to the sidecar AND patches the ChromaDB section metadata
// (so the list and retrieval filters reflect the change — see corpus router).
export function EditMetadataDialog({
  cv,
  onClose,
  onDone,
}: {
  cv: CVItem;
  onClose: () => void;
  onDone: () => void;
}) {
  const [metadata, setMetadata] = useState<CvMetadataFields>(blankMetadata(cv.filename));
  const [touched, setTouched] = useState<Set<keyof CvMetadataFields>>(new Set());
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .cvMetadata(cv.filename)
      .then((m) => setMetadata({ ...m, filename: cv.filename }))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [cv.filename]);

  const errors = validateMetadata(metadata);
  const shownErrors = Object.fromEntries(
    Object.entries(errors).filter(([k]) => touched.has(k as keyof CvMetadataFields)),
  );

  async function onSave() {
    setTouched(new Set(Object.keys(metadata) as (keyof CvMetadataFields)[]));
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await api.editMetadata(cv.filename, { ...metadata, filename: cv.filename });
      onDone();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Edit metadata — ${cv.display_name}`}
      description="Updates the sidecar and section metadata. No re-ingestion."
      className="max-w-xl"
    >
      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {loading ? (
        <div className="py-8 text-center text-sm text-muted-foreground">Loading metadata…</div>
      ) : (
        <div className="space-y-4">
          <CvMetadataForm
            value={metadata}
            onChange={setMetadata}
            errors={shownErrors}
            onBlurField={(f) => setTouched((t) => new Set(t).add(f))}
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button disabled={busy} onClick={() => void onSave()}>
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} Save
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
