import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { api, type Capabilities } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

// Shared owner-unlock state (D-38/D-39). ONE signed HttpOnly capability cookie authorises
// both full (Sonnet) runs (§12.7) and corpus writes (§12.8), so the unlock dialog + the
// `api.capabilities()` fetch live here once and both RunPage and CorpusPage consume them.
// The raw key is never kept after submit; the browser sends the cookie automatically.
interface UnlockContextValue {
  caps: Capabilities | null;
  configured: boolean; // server has an owner key → unlocking is possible at all
  unlocked: boolean; // this session holds a valid capability cookie
  refresh: () => Promise<void>;
  // Opens the unlock dialog if needed; resolves true once unlocked (immediately if already
  // unlocked), false if the user cancels. Callers gate an action on the resolved boolean.
  requestUnlock: () => Promise<boolean>;
  lock: () => Promise<void>;
}

const UnlockContext = createContext<UnlockContextValue | null>(null);

export function useUnlock(): UnlockContextValue {
  const ctx = useContext(UnlockContext);
  if (!ctx) throw new Error("useUnlock must be used within <UnlockProvider>");
  return ctx;
}

export function UnlockProvider({ children }: { children: ReactNode }) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCaps(await api.capabilities());
    } catch {
      setCaps(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const unlocked = !!caps?.full_unlocked;
  const configured = !!caps?.full_configured;

  const settle = useCallback((ok: boolean) => {
    resolver.current?.(ok);
    resolver.current = null;
  }, []);

  const requestUnlock = useCallback(() => {
    if (unlocked) return Promise.resolve(true);
    setErr(null);
    setKey("");
    setOpen(true);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, [unlocked]);

  function cancel() {
    setOpen(false);
    settle(false);
  }

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      await api.unlockFullMode(key);
      setKey(""); // never retain the raw key
      await refresh();
      setOpen(false);
      settle(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const lock = useCallback(async () => {
    try {
      await api.lockFullMode();
    } catch {
      /* best-effort */
    }
    await refresh();
  }, [refresh]);

  return (
    <UnlockContext.Provider value={{ caps, configured, unlocked, refresh, requestUnlock, lock }}>
      {children}
      <Dialog
        open={open}
        onClose={cancel}
        title="Unlock owner access"
        description="Editing the corpus and running full mode require the owner key. Enter it once — it's exchanged for a session cookie and never stored in the browser."
        className="max-w-md"
      >
        <div className="space-y-4">
          {err && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {err}
            </div>
          )}
          <input
            type="password"
            value={key}
            autoFocus
            placeholder="FULL_MODE_KEY"
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && key) void submit();
            }}
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={cancel}>
              Cancel
            </Button>
            <Button disabled={busy || !key} onClick={() => void submit()}>
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} Unlock
            </Button>
          </div>
        </div>
      </Dialog>
    </UnlockContext.Provider>
  );
}
