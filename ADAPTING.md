# Adapting cv-tailor to your own CV corpus

This project tailors *your* CVs to a job description. To use it with your own
material you need: a set of CV `.docx` files, API keys for three providers, and
Docker.

## 1. Authoring conventions your `.docx` files must follow (D-15)

Ingestion parses section boundaries from **Word heading styles**, not from font
size or bold text. This is a hard precondition:

- Use **Heading 1** (and optionally **Heading 2**) for section titles
  (Profile, Skills, Experience, etc.) — not manually enlarged/bolded text.
- The parser will **fail silently** on manually formatted headings: it produces
  a wrong-but-plausible corpus (e.g. 2 sections where 8 exist) without crashing.
- Always check the section inventory printed at ingestion before tailoring.
  Fewer than ~4 sections on a 2-page CV almost certainly means a parse failure.

## 2. Run ingestion first — it derives your length budgets (D-14)

Section length limits are **not hardcoded**. After ingesting all CVs, the
ingester computes a `SectionBudget` (min / max / median words) per section type
from your actual corpus and writes `budgets.yaml`. The median is the drafting
target; the max is the two-page guardrail enforced in the critique loop.

So: **ingest before any tailoring run.** `budgets.yaml` shows you exactly what
the system inferred about your section lengths.

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY, OPENAI_API_KEY, MISTRAL_API_KEY, FULL_MODE_KEY
docker compose build
docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
# review the section inventory + budgets.yaml, then:
docker compose run --rm cli python -m tailor run --jd data/jd.txt --demo
```

## 3. Re-ingesting after CV changes

Section structure is discovered once and persisted (ChromaDB metadata + YAML
front-matter); the tailoring path never re-derives it. If you change a CV's
section structure, re-ingest that file with `--replace`.
