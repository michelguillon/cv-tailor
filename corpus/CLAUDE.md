# CLAUDE.md — corpus/ (ingestion + retrieval)

Embeds CV sections into ChromaDB with metadata and serves metadata-filtered
semantic retrieval. Week 1 RAG reuse. Read the root `CLAUDE.md` first.

- `ingest.py` — CLI: parse `.docx`, derive budgets, embed, store. Also the
  per-CV, API-facing primitives the corpus UI composes (D-36/F-42):
  `parse_sections`, `preview_upload` (inventory, no writes), `commit_upload`
  (embed before the destructive delete-by-filename so a failed embed never
  half-replaces), `delete_cv`, `update_cv_metadata`, `write_sidecar`,
  `derive_budgets_from_collection`. The CLI `main()` and the UI share these — do
  not fork a parallel ingest path.
- `retrieval.py` — metadata pre-filter → semantic scoring.
- `metadata.py` — `CVMetadata` + YAML front-matter parser. `build_metadata`
  (sidecar) and `build_metadata_from_fields` (UI form dict) converge on identical
  `CVMetadata` via the same `validate_sidecar` (R-09).

## UI write path (D-36/F-42)

- **Edit Metadata patches ChromaDB, never re-embeds.** CV-level metadata is
  replicated onto every section document; the list + retrieval filters read it
  from there, so `update_cv_metadata` rewrites those fields via
  `collection.update` (metadata only). `skills_emphasis` is sidecar-only (not a
  stored ChromaDB field) — it is not patched there.
- **Budgets re-derive from ChromaDB metadata** (`derive_budgets_from_collection`),
  not by re-parsing every `.docx` — word counts are persisted at ingestion
  (R-10), so the numbers are identical with no re-load.

## Hard rules (each maps to a learned failure mode)

- **Unit of ingestion is a section, not a CV** (D-12, R-10). One ChromaDB
  document per section; CV-level metadata replicated onto every section document.
- **Parse by title vocabulary + size, verify loudly (D-19, F-04/F-05).** Section
  boundaries are detected by matching title text against `config.section_aliases`
  when the line is visually elevated (size > body, Heading-styled, or bold) —
  NOT by heading style alone (the corpus mixes Heading 1/3/4 + bold Normal for the
  same roles). Inside experience, the largest non-bullet size = company; split per
  company AND per role-group (D-21). A silent partial parse is the dangerous
  failure (R-01): print a section inventory (`section_id: N words`), report any
  matched-but-empty header, and **block ingestion** if any CV yields fewer than
  ~`MIN_SECTIONS` until a human confirms. `docx_loader.py` is reused from the
  Week 1 RAG pipeline (table-aware; do not "fix" it back to heading-only parsing).
- **ChromaDB metric is immutable** (R-03). Encode it in the collection name
  (`cv_sections_cosine`) and, after `get_or_create`, assert
  `collection.metadata["hnsw:space"] == config.metric`; raise with a clear
  "run --replace to recreate" message on mismatch.
- **Sanitise metadata** (R-04). `sanitise_metadata(d) -> dict` strips `None` and
  empty-string values before every `collection.add()` — ChromaDB rejects them
  silently in some versions. Absence of a key carries the "not set" meaning.
- **De-dup before embedding** (D-10). Key = `filename + version_date`. Skip
  matches without `--replace`; delete+re-add with it. Check *before* any
  embedding call to avoid wasted API spend.
- **Checkpoint per section** (R-06) and wrap every embedding call in
  `call_with_retry()` (R-05).
- **Discover once, persist, never re-derive** (R-10). Section structure lives in
  ChromaDB metadata + YAML front-matter; the tailoring path treats it as ground
  truth. Structure changed? Re-ingest with `--replace`.
- **Semantic-only retrieval.** Do not add BM25/hybrid at this corpus size —
  empirically semantic wins on small, paraphrase-rich corpora (R-07). Revisit
  only past ~200 sections.
- **`budgets.yaml` is derived here** (D-14): min/max/median words per
  section_type across the corpus. Written after a successful ingestion.
