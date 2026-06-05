"""api/routers/corpus.py — corpus management (SPEC §12.1, Mode 1). UI Step 2.

Read views + delete over the ingested ChromaDB corpus, wrapping corpus.retrieval /
corpus.ingest (imported, never shelled — RFI 15/16). The retrieval/ingest helpers
are imported at module scope so tests monkeypatch them and run without ChromaDB.

Upload + SSE-progress ingestion (which embeds via Mistral) is the one corpus action
that spends; it lands as a focused follow-on so this step stays read/manage-only.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, HTTPException

from corpus.ingest import get_collection, load_config
from corpus.retrieval import all_sections, collection_stats

router = APIRouter(prefix="/api/corpus", tags=["corpus"])


def _section_view(s: dict) -> dict:
    return {
        "section_id": s["section_id"], "section_type": s["section_type"],
        "word_count": s.get("word_count"), "static": bool(s.get("static")),
        "title": s.get("title") or s.get("company") or s["section_id"],
    }


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
            "filename": filename,
            "cv_type": first.get("cv_type"),
            "target_role": first.get("target_role"),
            "seniority": first.get("seniority"),
            "version_date": first.get("version_date"),
            "section_count": len(ordered),
            "sections": [_section_view(s) for s in ordered],
        })
    return out


@router.delete("/cvs/{filename}")
def delete_cv(filename: str) -> dict:
    """Remove all of a CV's sections from ChromaDB (SPEC §12.1 delete)."""
    config = load_config()
    collection = get_collection(config)
    existing = collection.get(where={"filename": filename})
    if not existing["ids"]:
        raise HTTPException(status_code=404, detail=f"no CV {filename!r} in the corpus")
    collection.delete(where={"filename": filename})
    return {"deleted": filename, "sections_removed": len(existing["ids"])}
