"""corpus/ingest.py — embed CV sections into ChromaDB + derive budgets.yaml.

CLI (run in the container):
    docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/ [--replace] [--yes]

Pipeline per CV: load_docx → sectionise → inventory. After showing the inventory
for every CV, a human confirmation gate (R-01) precedes any embedding or write —
a silently-wrong corpus is worse than a crash. Then: de-dup check (D-10), embed
(Mistral, retry-wrapped, R-05), store in ChromaDB (metric verified R-03, metadata
sanitised R-04), and derive per-section_type budgets (D-14) to budgets.yaml.

Discovery is persisted (R-10): structure + metadata land in ChromaDB and are
treated as ground truth by the tailoring path — never re-derived at runtime.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import yaml

from tailor.helpers import embed_texts
from tailor.models import CVMetadata

from .docx_loader import load_docx
from .metadata import build_metadata, sidecar_path, validate_sidecar, load_sidecar
from .sectioniser import MIN_SECTIONS, ExtractedSection, detect_headers, sectionise

CONFIG_PATH = Path("config.yaml")
BUDGETS_PATH = Path("budgets.yaml")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def sanitise_metadata(d: dict) -> dict:
    """Strip None and empty-string values — ChromaDB rejects them (R-04).

    Absence of a key carries the "not set" meaning (e.g. a generic CV has no
    target_company), so filtered retrieval still works for CVs that do set it.
    """
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _doc_id(filename: str, section_id: str) -> str:
    return f"{Path(filename).stem}::{section_id}"


def _dedup_key(meta: CVMetadata) -> str:
    return f"{meta.filename}::{meta.version_date}"


def _embedding_text(meta: CVMetadata, es: ExtractedSection) -> str:
    """Contextualise a section for embedding: role + section + title, then body."""
    head = f"{meta.target_role} — {es.section.section_type}: {es.title}".strip(" —:")
    return f"{head}\n{es.text}".strip()


def _section_metadatas(meta: CVMetadata, es: ExtractedSection) -> dict:
    return sanitise_metadata({
        "filename": meta.filename,
        "cv_type": meta.cv_type,
        "target_role": meta.target_role,
        "target_company": meta.target_company,   # omitted when None (generic CV)
        "seniority": meta.seniority,
        "version_date": meta.version_date,
        "dedup_key": _dedup_key(meta),
        "section_id": es.section.section_id,
        "section_type": es.section.section_type,
        "position": es.section.position,
        "static": es.section.static,
        "word_count": es.section.word_count,
        "line_count": es.section.line_count,
        "title": es.title,
        "company": es.company,            # "" for non-experience → dropped by sanitise
    })


# --------------------------------------------------------------------------- #
# Parse + inventory (no API, no writes)                                       #
# --------------------------------------------------------------------------- #

class ParsedCV:
    def __init__(self, path: Path, sections: list[ExtractedSection], meta: CVMetadata,
                 empty_headers: list[str], warnings: list[str]):
        self.path = path
        self.sections = sections
        self.meta = meta
        self.empty_headers = empty_headers
        self.warnings = warnings


def parse_cv(path: Path, config: dict) -> ParsedCV:
    paras = load_docx(path)
    sections = sectionise(paras, config["section_aliases"], config["static_sections"])
    # Reconcile matched headers vs emitted sections → report empty-but-matched (R-01).
    matched = set(detect_headers(paras, config["section_aliases"]))
    emitted = {es.section.section_type for es in sections}
    empty_headers = sorted(matched - emitted)
    # Sidecar metadata (raises if missing/invalid) + soft warnings surfaced here.
    raw = load_sidecar(path)
    _errors, warnings = validate_sidecar(raw)
    meta = build_metadata(path, [es.section for es in sections])
    return ParsedCV(path, sections, meta, empty_headers, warnings)


def print_inventory(parsed: list[ParsedCV]) -> bool:
    """Print the section inventory per CV. Returns True if all CVs look sane."""
    all_ok = True
    for p in parsed:
        n = len(p.sections)
        flag = "  ⚠ BELOW MINIMUM — likely a parse failure" if n < MIN_SECTIONS else ""
        print(f"\n── {p.path.name}  ({n} sections){flag}")
        print(f"   role={p.meta.target_role!r}  seniority={p.meta.seniority}  "
              f"type={p.meta.cv_type}  company={p.meta.target_company!r}")
        for es in p.sections:
            s = es.section
            tag = "static" if s.static else "active"
            print(f"     [{s.position:2}] {s.section_id:48} {tag:6} {s.word_count:4}w")
        for h in p.empty_headers:
            print(f"     · matched header '{h}' had no body → skipped (empty section)")
        for w in p.warnings:
            print(f"     ⚠ sidecar: {w}")
        if n < MIN_SECTIONS:
            all_ok = False
    return all_ok


# --------------------------------------------------------------------------- #
# ChromaDB                                                                    #
# --------------------------------------------------------------------------- #

def get_collection(config: dict, collection_name: str | None = None):
    """Open the persistent collection, verifying the immutable metric (R-03)."""
    import chromadb

    chroma_cfg = config["chroma"]
    name = collection_name or chroma_cfg["collection"]
    metric = chroma_cfg["metric"]
    client = chromadb.PersistentClient(path=chroma_cfg["persist_dir"])
    collection = client.get_or_create_collection(name=name, metadata={"hnsw:space": metric})
    actual = (collection.metadata or {}).get("hnsw:space")
    if actual != metric:
        raise RuntimeError(
            f"Collection '{name}' exists with metric {actual!r}, config requires {metric!r}. "
            f"Run with --replace to recreate, or delete data/chroma."
        )
    return collection


# --------------------------------------------------------------------------- #
# Ingestion                                                                   #
# --------------------------------------------------------------------------- #

def ingest_cv(collection, parsed: ParsedCV, embed_model: str, replace: bool) -> tuple[int, int]:
    """Embed + store one CV's sections. Returns (sections_written, embed_tokens)."""
    meta = parsed.meta
    existing = collection.get(where={"dedup_key": _dedup_key(meta)})
    if existing["ids"]:
        if not replace:
            print(f"   skip {meta.filename}: already ingested "
                  f"({len(existing['ids'])} sections, version {meta.version_date}). Use --replace.")
            return 0, 0
        collection.delete(where={"dedup_key": _dedup_key(meta)})
        print(f"   --replace: removed {len(existing['ids'])} existing sections for {meta.filename}")

    texts = [_embedding_text(meta, es) for es in parsed.sections]
    vectors, tokens = embed_texts(texts, model=embed_model)   # retry-wrapped (R-05)
    collection.add(
        ids=[_doc_id(meta.filename, es.section.section_id) for es in parsed.sections],
        embeddings=vectors,
        documents=[es.text for es in parsed.sections],
        metadatas=[_section_metadatas(meta, es) for es in parsed.sections],
    )
    print(f"   stored {len(parsed.sections)} sections for {meta.filename} ({tokens} embed tokens)")
    return len(parsed.sections), tokens


def derive_budgets(parsed: list[ParsedCV]) -> dict:
    """Per-section_type min/max/median word counts across the corpus (D-14)."""
    by_type: dict[str, list[int]] = {}
    for p in parsed:
        for es in p.sections:
            by_type.setdefault(es.section.section_type, []).append(es.section.word_count)
    budgets = {}
    for section_type, counts in sorted(by_type.items()):
        budgets[section_type] = {
            "min_words": min(counts),
            "max_words": max(counts),
            "target_words": int(statistics.median(counts)),
        }
    return budgets


def write_budgets(budgets: dict, path: Path = BUDGETS_PATH) -> None:
    header = (
        "# budgets.yaml — derived at ingestion from observed corpus word counts (D-14).\n"
        "# Do not hand-edit; regenerated by `python -m corpus.ingest`.\n"
        "# target_words (median) is the drafting target; max_words a hard ceiling for critique.\n"
    )
    path.write_text(header + yaml.safe_dump(budgets, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="corpus.ingest", description="Ingest CV corpus into ChromaDB.")
    ap.add_argument("--cv-dir", type=Path, required=True, help="Directory of CV .docx files")
    ap.add_argument("--collection", default=None, help="ChromaDB collection name override")
    ap.add_argument("--replace", action="store_true", help="Re-ingest CVs that already exist")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation gate (non-interactive)")
    args = ap.parse_args(argv)

    config = load_config()
    embed_model = config["models"]["embeddings"]

    docx_files = sorted(args.cv_dir.glob("*.docx"))
    if not docx_files:
        print(f"No .docx files in {args.cv_dir}", file=sys.stderr)
        return 1

    # 1. Parse + inventory (no API, no writes) — fail loud before spending anything.
    try:
        parsed = [parse_cv(p, config) for p in docx_files]
    except (FileNotFoundError, ValueError) as exc:
        print(f"\nIngestion blocked: {exc}", file=sys.stderr)
        return 1

    print(f"Parsed {len(parsed)} CVs from {args.cv_dir}:")
    all_ok = print_inventory(parsed)
    if not all_ok:
        print("\nIngestion blocked: a CV parsed below the section minimum. "
              "Fix the .docx (heading/size structure) and retry.", file=sys.stderr)
        return 1

    # 2. Human confirmation gate (R-01).
    if not args.yes:
        try:
            reply = input("\nProceed to embed + store these sections? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Aborted before any API spend or writes.")
            return 0

    # 3. Embed + store (per-CV checkpoint: ChromaDB persists after each CV).
    collection = get_collection(config, args.collection)
    total_sections = total_tokens = 0
    for p in parsed:
        written, tokens = ingest_cv(collection, p, embed_model, args.replace)
        total_sections += written
        total_tokens += tokens

    # 4. Derive + write budgets (from all parsed CVs, independent of skip/replace).
    write_budgets(derive_budgets(parsed))

    # Notional list-price estimate only (mistral-embed ≈ $0.10 / 1M tokens). This is
    # NOT a bill: the free "Experiment" tier costs nothing. Shown for the cost-
    # tracking story (D-08) — what it would cost on the paid tier.
    est_cost = total_tokens / 1_000_000 * 0.10
    print(f"\nDone. {total_sections} sections written, {total_tokens} embed tokens "
          f"(est. ${est_cost:.4f} at paid list price; $0 on the free tier). budgets.yaml updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
