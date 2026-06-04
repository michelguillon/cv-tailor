"""corpus/retrieval.py — metadata-filtered semantic search over CV sections.

Phase 1 (fit assessment) embeds JD requirements and retrieves the best-matching
sections per section_type. Pure semantic scoring — no BM25/hybrid at this corpus
size (R-07). `section_type` and `cv_type` are hard `where` filters; **seniority is
deliberately NOT a hard filter** (D-23) — it's a ranking preference applied by the
caller, so a strong generic CV is never excluded by a JD's noisy seniority signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailor.helpers import embed_query

from .ingest import get_collection, load_config

__all__ = ["SectionHit", "search_sections", "build_where", "collection_stats"]


@dataclass
class SectionHit:
    section_id: str
    section_type: str
    filename: str
    cv_type: str
    seniority: str
    title: str
    document: str
    distance: float       # lower = closer (cosine distance)
    metadata: dict

    @property
    def score(self) -> float:
        """Similarity in [0,1] from cosine distance (higher = better)."""
        return max(0.0, 1.0 - self.distance)


def build_where(section_type: str | None = None, cv_type: str | None = None) -> dict | None:
    """ChromaDB `where` filter. Seniority is intentionally excluded (D-23)."""
    clauses = []
    if section_type is not None:
        clauses.append({"section_type": section_type})
    if cv_type is not None:
        clauses.append({"cv_type": cv_type})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def search_sections(
    query: str,
    *,
    n_results: int = 5,
    section_type: str | None = None,
    cv_type: str | None = None,
    where: dict | None = None,
    config: dict | None = None,
    collection=None,
) -> list[SectionHit]:
    """Embed the query and return the closest sections, optionally metadata-filtered."""
    config = config or load_config()
    collection = collection if collection is not None else get_collection(config)
    where = where if where is not None else build_where(section_type, cv_type)

    vector = embed_query(query, model=config["models"]["embeddings"])
    res = collection.query(
        query_embeddings=[vector],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    hits: list[SectionHit] = []
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]
    for _id, doc, meta, dist in zip(ids, docs, metas, dists):
        hits.append(SectionHit(
            section_id=meta.get("section_id", _id),
            section_type=meta.get("section_type", ""),
            filename=meta.get("filename", ""),
            cv_type=meta.get("cv_type", ""),
            seniority=meta.get("seniority", ""),
            title=meta.get("title", ""),
            document=doc,
            distance=float(dist),
            metadata=meta,
        ))
    return hits


def collection_stats(config: dict | None = None) -> dict:
    """Counts for sanity/verification: total sections + per section_type."""
    config = config or load_config()
    collection = get_collection(config)
    got = collection.get(include=["metadatas"])
    by_type: dict[str, int] = {}
    for meta in got["metadatas"]:
        st = meta.get("section_type", "?")
        by_type[st] = by_type.get(st, 0) + 1
    return {"total": len(got["ids"]), "by_section_type": dict(sorted(by_type.items()))}
