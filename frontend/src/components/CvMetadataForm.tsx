import { useState } from "react";
import { X } from "lucide-react";
import type { CvMetadataFields } from "@/lib/api";
import { cn } from "@/lib/utils";

export const CV_TYPES = ["generic", "job_specific"] as const;
export const SENIORITY_LEVELS = ["senior", "principal", "director", "vp"] as const;

export function todayISO(): string {
  return new Date().toISOString().slice(0, 10); // YYYY-MM-DD
}

export function blankMetadata(filename = ""): CvMetadataFields {
  return {
    filename,
    cv_type: "job_specific",
    target_role: "",
    target_company: null,
    skills_emphasis: [],
    seniority: "principal",
    version_date: todayISO(),
  };
}

// Field-level validation mirroring the backend's validate_sidecar (R-09) so the user
// sees errors inline before submit. Returns only the fields that are invalid.
export function validateMetadata(v: CvMetadataFields): Partial<Record<keyof CvMetadataFields, string>> {
  const errors: Partial<Record<keyof CvMetadataFields, string>> = {};
  if (!v.target_role.trim()) errors.target_role = "Target role is required.";
  if (!(CV_TYPES as readonly string[]).includes(v.cv_type)) errors.cv_type = "Pick a CV type.";
  if (!(SENIORITY_LEVELS as readonly string[]).includes(v.seniority))
    errors.seniority = "Pick a seniority level.";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v.version_date) || Number.isNaN(Date.parse(v.version_date)))
    errors.version_date = "Use a valid date (YYYY-MM-DD).";
  return errors;
}

const labelCls = "block text-sm font-medium";
const inputCls =
  "mt-1 w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const errCls = "mt-1 text-xs text-destructive";

export function CvMetadataForm({
  value,
  onChange,
  errors,
  onBlurField,
}: {
  value: CvMetadataFields;
  onChange: (v: CvMetadataFields) => void;
  errors: Partial<Record<keyof CvMetadataFields, string>>;
  onBlurField?: (field: keyof CvMetadataFields) => void;
}) {
  const set = <K extends keyof CvMetadataFields>(k: K, val: CvMetadataFields[K]) =>
    onChange({ ...value, [k]: val });

  return (
    <div className="space-y-4">
      <div>
        <label className={labelCls}>Filename</label>
        <input className={cn(inputCls, "text-muted-foreground")} value={value.filename} readOnly />
        <p className="mt-1 text-xs text-muted-foreground">Derived from the uploaded file.</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className={labelCls}>CV type</label>
          <select
            className={inputCls}
            value={value.cv_type}
            onChange={(e) => set("cv_type", e.target.value as CvMetadataFields["cv_type"])}
            onBlur={() => onBlurField?.("cv_type")}
          >
            {CV_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {errors.cv_type && <p className={errCls}>{errors.cv_type}</p>}
        </div>
        <div>
          <label className={labelCls}>Seniority</label>
          <select
            className={inputCls}
            value={value.seniority}
            onChange={(e) => set("seniority", e.target.value as CvMetadataFields["seniority"])}
            onBlur={() => onBlurField?.("seniority")}
          >
            {SENIORITY_LEVELS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {errors.seniority && <p className={errCls}>{errors.seniority}</p>}
        </div>
      </div>

      <div>
        <label className={labelCls}>
          Target role <span className="text-destructive">*</span>
        </label>
        <input
          className={inputCls}
          value={value.target_role}
          placeholder="e.g. Solutions Engineering Leadership"
          onChange={(e) => set("target_role", e.target.value)}
          onBlur={() => onBlurField?.("target_role")}
        />
        {errors.target_role && <p className={errCls}>{errors.target_role}</p>}
      </div>

      <div>
        <label className={labelCls}>Target company</label>
        <input
          className={inputCls}
          value={value.target_company ?? ""}
          placeholder="Blank for a generic CV"
          onChange={(e) => set("target_company", e.target.value.trim() === "" ? null : e.target.value)}
          onBlur={() => onBlurField?.("target_company")}
        />
      </div>

      <div>
        <label className={labelCls}>Skills emphasis</label>
        <ChipInput value={value.skills_emphasis} onChange={(chips) => set("skills_emphasis", chips)} />
        <p className="mt-1 text-xs text-muted-foreground">Press Enter or comma to add. Optional.</p>
      </div>

      <div>
        <label className={labelCls}>Version date</label>
        <input
          type="date"
          className={inputCls}
          value={value.version_date}
          onChange={(e) => set("version_date", e.target.value)}
          onBlur={() => onBlurField?.("version_date")}
        />
        {errors.version_date && <p className={errCls}>{errors.version_date}</p>}
      </div>
    </div>
  );
}

function ChipInput({ value, onChange }: { value: string[]; onChange: (chips: string[]) => void }) {
  const [draft, setDraft] = useState("");

  const add = (raw: string) => {
    const v = raw.trim();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  };

  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1.5">
      {value.map((chip) => (
        <span
          key={chip}
          className="inline-flex items-center gap-1 rounded bg-accent px-2 py-0.5 text-xs text-accent-foreground"
        >
          {chip}
          <button onClick={() => onChange(value.filter((c) => c !== chip))} aria-label={`Remove ${chip}`}>
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
      <input
        className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 text-sm focus-visible:outline-none"
        value={draft}
        placeholder={value.length ? "" : "AI, pre-sales, …"}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add(draft);
          } else if (e.key === "Backspace" && draft === "" && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={() => draft && add(draft)}
      />
    </div>
  );
}
