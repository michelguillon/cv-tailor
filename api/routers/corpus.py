"""api/routers/corpus.py — corpus management (SPEC §12.1, Mode 1 / D-36). UI Step 2.

Read views + the full CV lifecycle (add / edit metadata / replace / delete) over the
ingested ChromaDB corpus, wrapping corpus.ingest / corpus.retrieval (imported, never
shelled — RFI 15/16). The ingest/retrieval helpers are imported at module scope so
tests monkeypatch them and run without ChromaDB or Mistral.

Ingestion is two HTTP steps behind the load-bearing R-01/D-36 gate: **upload** stages
the .docx and returns its parsed section inventory (no writes); **confirm** embeds +
stores it only after the human has seen that inventory. It is synchronous JSON, not
SSE — one CV is a single batched embed call, and the human checkpoint is preview→
confirm, not progress-watching (F-xx). The UI form replaces the .yaml sidecar; confirm
writes the equivalent sidecar so CLI and UI produce identical on-disk state (D-36).
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from api.security import require_unlocked
from corpus.ingest import (
    CV_DIR,
    commit_upload,
    derive_budgets_from_collection,
    get_collection,
    load_config,
    preview_upload,
    update_cv_metadata,
    write_budgets,
    write_sidecar,
)
from corpus.ingest import delete_cv as ingest_delete_cv
from corpus.metadata import sidecar_path, validate_sidecar
from corpus.retrieval import all_sections, collection_stats
from tailor.config import cv_display_name

router = APIRouter(prefix="/api/corpus", tags=["corpus"])

# Pre-confirm staging: the uploaded .docx lives here until the human confirms the
# section inventory, then moves to CV_DIR. A best-effort TTL sweep on each upload
# drops abandoned uploads (mirrors the SessionStore TTL intent, decoupled from runs).
TMP_CORPUS = Path("tmp/corpus")
TMP_TTL_SECONDS = 3600.0


def _section_view(s: dict) -> dict:
    return {
        "section_id": s["section_id"], "section_type": s["section_type"],
        "word_count": s.get("word_count"), "static": bool(s.get("static")),
        "title": s.get("title") or s.get("company") or s["section_id"],
    }


def _sweep_tmp(now: float | None = None) -> None:
    """Drop staged uploads older than the TTL (abandoned before confirm)."""
    now = time.time() if now is None else now
    if not TMP_CORPUS.exists():
        return
    for d in TMP_CORPUS.iterdir():
        try:
            if d.is_dir() and (now - d.stat().st_mtime) > TMP_TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _parse_metadata(metadata: str, filename: str) -> dict:
    """Decode the form's metadata JSON and stamp the authoritative filename."""
    try:
        fields = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"metadata is not valid JSON: {exc}")
    if not isinstance(fields, dict):
        raise HTTPException(status_code=400, detail="metadata must be a JSON object")
    fields["filename"] = filename            # the uploaded file's name is authoritative
    return fields


def _validate_or_422(fields: dict) -> None:
    errors, _warnings = validate_sidecar(fields)
    if errors:
        raise HTTPException(status_code=422, detail="Invalid metadata: " + "; ".join(errors))


# --------------------------------------------------------------------------- #
# Read views                                                                  #
# --------------------------------------------------------------------------- #

@router.get("/stats")
def corpus_stats() -> dict:
    """Corpus summary for the landing page: CV count, section count, per-type counts."""
    config = load_config()
    stats = collection_stats(config)                       # {total, by_section_type}
    sections = all_sections(config)
    filenames = {s["filename"] for s in sections}
    version_dates = sorted({s.get("version_date") for s in sections if s.get("version_date")})
    return {
        "cv_count": len(filenames),
        "section_count": stats["total"],
        "by_section_type": stats["by_section_type"],
        "last_ingested": version_dates[-1] if version_dates else None,
    }


@router.get("/cvs")
def list_cvs() -> list[dict]:
    """Every CV with its section breakdown (the corpus inventory, SPEC §12.1)."""
    config = load_config()
    by_cv: dict[str, list[dict]] = defaultdict(list)
    for s in all_sections(config):
        by_cv[s["filename"]].append(s)
    out = []
    for filename, ss in sorted(by_cv.items()):
        ordered = sorted(ss, key=lambda s: s.get("position", 0))
        first = ordered[0]
        out.append({
            "filename": filename,                              # real key (delete/retrieval)
            "display_name": cv_display_name(config, filename),  # company-name-free UI label (F-41)
            "cv_type": first.get("cv_type"),
            "target_role": first.get("target_role"),
            "target_company": first.get("target_company"),      # absent on generic CVs (sanitised out)
            "seniority": first.get("seniority"),
            "version_date": first.get("version_date"),
            "section_count": len(ordered),
            "sections": [_section_view(s) for s in ordered],
        })
    return out


@router.get("/cvs/{filename}/metadata")
def get_cv_metadata(filename: str) -> dict:
    """Editorial metadata for form pre-fill (Edit / Replace flows).

    The sidecar is the source of truth for editorial fields (it alone carries
    `skills_emphasis`, which is not a stored ChromaDB field); fall back to the
    ChromaDB section metadata if the sidecar is missing."""
    path = sidecar_path(CV_DIR / filename)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["filename"] = filename
        return data
    # Fallback: reconstruct what we can from ChromaDB (no skills_emphasis there).
    for s in all_sections(load_config()):
        if s["filename"] == filename:
            return {
                "filename": filename, "cv_type": s.get("cv_type"),
                "target_role": s.get("target_role"), "target_company": s.get("target_company"),
                "skills_emphasis": [], "seniority": s.get("seniority"),
                "version_date": s.get("version_date"),
            }
    raise HTTPException(status_code=404, detail=f"no CV {filename!r} in the corpus")


# --------------------------------------------------------------------------- #
# Add / Replace — two-step (upload → confirm) behind the R-01 inventory gate   #
#                                                                              #
# Every state-mutating endpoint below is gated on the owner capability cookie  #
# (`require_unlocked`, D-39/§12.8) — the same unlock as full mode. Reads above  #
# stay public; the dependency runs before the handler, so a refused write never #
# stages, parses, embeds, or indexes anything (403 fail-closed).               #
# --------------------------------------------------------------------------- #

def _stage_upload(file: UploadFile, fields: dict, *, replace: bool) -> dict:
    """Shared upload body: validate, (Add only) reject duplicates, stage, preview."""
    _sweep_tmp()
    filename = fields["filename"]
    _validate_or_422(fields)
    if not replace and (CV_DIR / filename).exists():
        raise HTTPException(
            status_code=409,
            detail="This CV is already in the corpus. Use Replace to update it.",
        )
    token = uuid.uuid4().hex
    dest_dir = TMP_CORPUS / token
    dest_dir.mkdir(parents=True, exist_ok=True)
    staged = dest_dir / filename
    staged.write_bytes(file.file.read())

    preview = preview_upload(staged, fields, load_config())
    return {"token": token, "filename": filename, "replace": replace, **preview}


@router.post("/upload", dependencies=[Depends(require_unlocked)])
def upload_cv(file: UploadFile = File(...), metadata: str = Form(...),
              replace: bool = Form(False)) -> dict:
    """Stage a new CV and return its parsed section inventory (Add flow, no writes)."""
    fields = _parse_metadata(metadata, file.filename or "")
    return _stage_upload(file, fields, replace=replace)


@router.post("/replace", dependencies=[Depends(require_unlocked)])
def replace_cv(file: UploadFile = File(...), metadata: str = Form(...)) -> dict:
    """Stage a replacement .docx (Replace flow) — same as upload, no duplicate check."""
    fields = _parse_metadata(metadata, file.filename or "")
    return _stage_upload(file, fields, replace=True)


class ConfirmRequest(BaseModel):
    token: str
    filename: str
    metadata: dict
    replace: bool = False


@router.post("/confirm", dependencies=[Depends(require_unlocked)])
def confirm_cv(body: ConfirmRequest) -> dict:
    """Commit a staged CV to ChromaDB after the human confirmed the inventory (D-36).

    Embed + store (Replace deletes the old version first), then move the .docx into
    `data/cvs/`, write the equivalent sidecar (CLI/UI parity), re-derive budgets.yaml
    from the corpus, and clean up the staged upload."""
    staged = TMP_CORPUS / body.token / body.filename
    if not staged.exists():
        raise HTTPException(status_code=410,
                            detail="Upload expired or was already committed. Re-upload the CV.")
    fields = dict(body.metadata)
    fields["filename"] = body.filename
    _validate_or_422(fields)

    config = load_config()
    collection = get_collection(config)
    result = commit_upload(staged, fields, config, replace=body.replace, collection=collection)

    CV_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(CV_DIR / body.filename))   # tmp → data/cvs on success
    write_sidecar(body.filename, fields, cv_dir=CV_DIR)
    write_budgets(derive_budgets_from_collection(collection))
    shutil.rmtree(TMP_CORPUS / body.token, ignore_errors=True)

    return {"status": "ok", "filename": body.filename,
            "sections_committed": result["sections_committed"],
            "removed": result["removed"], "replaced": body.replace}


# --------------------------------------------------------------------------- #
# Edit metadata — sidecar + ChromaDB metadata patch (no re-ingest, D-36)       #
# --------------------------------------------------------------------------- #

class MetadataPatch(BaseModel):
    metadata: dict


@router.patch("/cvs/{filename}/metadata", dependencies=[Depends(require_unlocked)])
def patch_cv_metadata(filename: str, body: MetadataPatch) -> dict:
    """Update a CV's editorial metadata in place — no re-embedding, no inventory gate.

    Writes the sidecar AND patches the ChromaDB section metadata (the list and
    retrieval filters read it from there, so a sidecar-only edit would be inert, F-xx)."""
    fields = dict(body.metadata)
    fields["filename"] = filename
    _validate_or_422(fields)

    config = load_config()
    updated = update_cv_metadata(filename, fields, config=config, collection=get_collection(config))
    if updated == 0:
        raise HTTPException(status_code=404, detail=f"no CV {filename!r} in the corpus")
    write_sidecar(filename, fields, cv_dir=CV_DIR)
    return {"status": "ok", "filename": filename, "sections_updated": updated}


# --------------------------------------------------------------------------- #
# Delete                                                                      #
# --------------------------------------------------------------------------- #

@router.delete("/cvs/{filename}", dependencies=[Depends(require_unlocked)])
def delete_cv(filename: str) -> dict:
    """Remove all of a CV's sections from ChromaDB (SPEC §12.1 delete)."""
    config = load_config()
    removed = ingest_delete_cv(filename, config=config, collection=get_collection(config))
    if removed == 0:
        raise HTTPException(status_code=404, detail=f"no CV {filename!r} in the corpus")
    return {"deleted": filename, "sections_removed": removed}
