"""api/routers/corpus.py — corpus management (SPEC §12.1, Mode 1).

UI Step 1: stub. UI Step 2 wires these to corpus.retrieval.collection_stats /
all_sections (view) and corpus.ingest (add/replace via SSE) and a delete that drops
a CV's sections from ChromaDB. Kept here so the route shape is fixed from the start.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/corpus", tags=["corpus"])


@router.get("/stats")
def corpus_stats() -> dict:
    raise HTTPException(status_code=501, detail="corpus stats land in UI Step 2")


@router.get("/cvs")
def list_cvs() -> dict:
    raise HTTPException(status_code=501, detail="CV inventory lands in UI Step 2")
