"""corpus/metadata.py — CVMetadata sidecar YAML (the human-authored half).

Section *structure* is discovered from the .docx (corpus/sectioniser.py). The
per-CV editorial metadata that a parser can't know — what role/company the CV
targets, its seniority, what it emphasises — is authored by the human in a
sidecar `<cvname>.yaml` next to the .docx (decision: sidecar for now).

`build_metadata` fuses the two: sidecar fields + discovered `CVSection` list →
a complete `CVMetadata`. `sidecar_template` emits a pre-filled stub (guesses
inferred from the filename) for the human to confirm at ingestion — the
discover-then-confirm gate (R-01).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tailor.models import CVMetadata, CVSection

__all__ = [
    "sidecar_path",
    "load_sidecar",
    "validate_sidecar",
    "build_metadata",
    "build_metadata_from_fields",
    "sidecar_template",
    "SIDECAR_FIELDS",
    "CV_TYPES",
    "SENIORITY_LEVELS",
]

# Human-authored fields (everything on CVMetadata except discovered `sections`).
SIDECAR_FIELDS = (
    "filename",
    "cv_type",
    "target_role",
    "target_company",
    "skills_emphasis",
    "seniority",
    "version_date",
)

# Controlled vocabularies — single scalar values (ChromaDB metadata is scalar,
# so these can't be lists; breadth for a generic CV is carried by content/
# semantics, not by cramming multiple values into the filter field). F-06.
CV_TYPES = {"generic", "job_specific"}
SENIORITY_LEVELS = {"senior", "principal", "director", "vp"}


def validate_sidecar(data: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a sidecar dict. Errors block ingestion.

    Catches the field-value mistakes that are invisible until retrieval: a
    multi-value seniority, an out-of-vocabulary level, a list-typed scalar, a
    company on a generic CV (R-09 — validate at write-time, not downstream).
    """
    errors: list[str] = []
    warnings: list[str] = []

    missing = [f for f in SIDECAR_FIELDS if f not in data]
    if missing:
        errors.append(f"missing field(s): {', '.join(missing)}")
        return errors, warnings  # can't validate values we don't have

    if data["cv_type"] not in CV_TYPES:
        errors.append(f"cv_type must be one of {sorted(CV_TYPES)}, got {data['cv_type']!r}")

    seniority = data["seniority"]
    if not isinstance(seniority, str) or seniority not in SENIORITY_LEVELS:
        errors.append(
            f"seniority must be a single value from {sorted(SENIORITY_LEVELS)}, got "
            f"{seniority!r} (a comma-list parses as one string and won't match)"
        )

    if not isinstance(data["target_role"], str) or not data["target_role"].strip():
        errors.append("target_role must be a non-empty string")

    if not isinstance(data["skills_emphasis"], list):
        errors.append("skills_emphasis must be a YAML list, e.g. [AI, pre-sales]")

    tc = data.get("target_company")
    if data.get("cv_type") == "generic" and tc not in (None, "", "null"):
        warnings.append(f"cv_type is 'generic' but target_company is set ({tc!r}); expected null")
    if data.get("cv_type") == "job_specific" and tc in (None, "", "null"):
        warnings.append("cv_type is 'job_specific' but target_company is null")

    return errors, warnings


def sidecar_path(docx_path: str | Path) -> Path:
    """The sidecar YAML path for a CV: `<name>.docx` → `<name>.yaml`."""
    p = Path(docx_path)
    return p.with_suffix(".yaml")


def load_sidecar(docx_path: str | Path) -> dict:
    """Load and validate a CV's sidecar YAML. Raises if missing or malformed."""
    path = sidecar_path(docx_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No sidecar metadata for {Path(docx_path).name}. Expected {path.name} "
            f"next to the .docx. Generate a template with `sidecar_template` and fill it in."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors, _warnings = validate_sidecar(data)
    if errors:
        raise ValueError(f"{path.name} is invalid:\n  - " + "\n  - ".join(errors))
    return data


def build_metadata_from_fields(fields: dict, sections: list[CVSection]) -> CVMetadata:
    """Fuse a metadata *dict* (the UI form path) + discovered sections into a CVMetadata.

    The sidecar-free sibling of `build_metadata`: the Corpus UI collects the same
    fields interactively instead of from a `.yaml` file (D-36), so both code paths
    converge on identical `CVMetadata`. Validates exactly as `load_sidecar` does —
    raises `ValueError` on any field error — so a bad form can't reach ChromaDB (R-09).
    """
    errors, _warnings = validate_sidecar(fields)
    if errors:
        raise ValueError("invalid metadata:\n  - " + "\n  - ".join(errors))
    return CVMetadata(
        filename=fields["filename"],
        cv_type=fields["cv_type"],
        target_role=fields["target_role"],
        target_company=fields.get("target_company") or None,
        skills_emphasis=list(fields.get("skills_emphasis") or []),
        seniority=fields["seniority"],
        version_date=str(fields["version_date"]),
        sections=sections,
    )


def build_metadata(docx_path: str | Path, sections: list[CVSection]) -> CVMetadata:
    """Fuse sidecar fields + discovered sections into a CVMetadata."""
    return build_metadata_from_fields(load_sidecar(docx_path), sections)


def _guess_from_filename(filename: str) -> dict:
    """Best-effort guesses to pre-fill a template, e.g. company from the filename.

    The human confirms/edits before ingestion — these are starting points, not
    ground truth.
    """
    stem = Path(filename).stem
    year = (re.search(r"(20\d{2})", stem) or [None, ""])[1] if re.search(r"20\d{2}", stem) else ""
    # Trailing token(s) after the year often name the target, e.g. "..._2026_Airwallex".
    tail = re.split(r"20\d{2}_?", stem)[-1].replace("_", " ").strip()
    return {"target_hint": tail, "year": year}


def sidecar_template(filename: str) -> str:
    """A commented YAML stub for a CV's sidecar, with filename-derived guesses."""
    g = _guess_from_filename(filename)
    hint = g["target_hint"]
    version = f"{g['year']}-01-01" if g["year"] else "2026-01-01"
    return (
        f"# Sidecar metadata for {filename} — review every field before ingesting.\n"
        f"# Single scalar values only (used as ChromaDB pre-filters); skills_emphasis is a list.\n"
        f"filename: {filename}\n"
        f"cv_type: job_specific        # \"generic\" | \"job_specific\"\n"
        f"target_role: \"\"              # ONE umbrella phrase, e.g. \"Solutions Engineering Leadership\"\n"
        f"                             #   (filename hint: {hint!r})\n"
        f"target_company: {hint!r}      # the company for a job_specific CV; null for a generic CV\n"
        f"skills_emphasis: []          # YAML list, e.g. [AI, solutions consulting, pre-sales]\n"
        f"seniority: principal         # ONE of: senior | principal | director | vp\n"
        f"version_date: \"{version}\"\n"
    )
