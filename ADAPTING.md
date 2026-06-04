# Adapting cv-tailor to your own CV corpus

This project tailors *your* CVs to a job description. To use it with your own
material you need: a set of CV `.docx` files, API keys for three providers, and
Docker.

## 1. Authoring conventions your `.docx` files must follow (D-19)

Ingestion detects section boundaries by **matching the section title text**
(Profile, Skills, Experience, Education, …) against a vocabulary in
`config.yaml` (`section_aliases`), as long as the title is visually elevated
(larger than body text, a Heading style, or bold). It does **not** rely on
heading styles being applied consistently — the real corpus mixes Heading 1,
Heading 3/4, and bold `Normal` for the same kinds of headers, so a style-only
parser would fail (see LEARNING_NOTES F-04/F-05). Practical rules:

- Make section titles **stand out** from body text (larger font, a Heading
  style, or bold) and use **recognisable names** — if you use a title that
  isn't in `section_aliases`, add it there.
- Inside Experience, **company names must be larger than role lines** (the
  parser treats the largest non-bullet size in the Experience block as the
  company level, and splits per company + per role-group below that).
- Each CV also needs a sidecar `<name>.yaml` of editorial metadata
  (`cv_type`, `target_role`, `seniority`, …) — generate a template by running
  ingestion once; it's validated on load.
- The parser can still mis-split an unusual layout. **Always check the section
  inventory printed at ingestion** before confirming. Fewer than ~4 sections on
  a 2-page CV almost certainly means a parse failure, and ingestion blocks until
  you confirm. Matched-but-empty headers are reported, not silently dropped.

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
