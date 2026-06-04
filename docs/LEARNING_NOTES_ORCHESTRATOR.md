# LEARNING_NOTES_ORCHESTRATOR.md — cv-tailor
## Architectural Decisions, Findings, and Portfolio Notes

**Project:** Week 3 Portfolio — Multi-Model Orchestration  
**Repository:** cv-tailor
**Status:** Pre-build (architectural decisions recorded; build not yet started)  
**Last updated:** pre-build

---

## How to use this document

Each entry records one architectural decision: what was decided, what was
rejected, the load-bearing reason, what the pattern generalises to, and how
to frame it in an interview. This is the "why" complement to SPEC_ORCHESTRATOR.md
(the "what").

Entries are added throughout the build — when a decision is made, when a
finding changes the design, when a test reveals something unexpected.

---

## Decision Log

---

### D-01 — Hybrid pipeline with agentic refinement loop

**What was decided:**
Deterministic phases (0–2, 4–6) frame an agentic refinement loop (Phase 3).
The pipeline always runs the same phases in the same order. Phase 3 is agentic:
the orchestrator decides what critique to accept, whether to extend the rubric,
and when convergence has been reached.

**Alternatives rejected:**
- *Pure pipeline* — fixing the revision sequence removes the system's ability
  to adapt based on what critique reveals. The number of iterations is genuinely
  unknown in advance.
- *Pure agentic loop* — unpredictable costs, no defined HITL placement, harder
  to test deterministically.

**Load-bearing reason:**
Determinism where the task is known; agency where judgment is required. The
framing phases (JD parsing, initial drafting, formatting validation) have
fixed inputs and fixed expected outputs. The refinement loop doesn't — it
terminates based on convergence signals that are only computable at runtime.

**What this generalises to:**
Most production agentic systems are not pure agent loops. They are deterministic
scaffolds containing one or more bounded agentic regions. The boundary between
"always does X" and "decides whether to do X" is an architectural choice with
cost, testability, and HITL-placement consequences. Making that boundary
explicit is a design skill.

**Interview framing:**
"I deliberately split the system into deterministic phases and an agentic loop
rather than making everything agentic. The refinement stage genuinely needs
model judgment — when to stop, which suggestions to accept. The fit assessment
and validation stages don't. Conflating them is a common design mistake."

**Open question resolved during architecture review:**
The "orchestrator judges convergence" soft-stop needs a concrete trigger, not
just a free-form model judgment. Agreed resolution: soft-stop is permitted only
when the last critique returned zero major-severity items. This makes the
soft-stop testable and observable in the audit trail.

---

### D-02 — LLMs as tools: the cross-provider abstraction

**What was decided:**
The orchestrator calls other LLMs as tools, identically to how the Week 2
agent called SQLite queries. `critique_cv(draft, jd_analysis)` internally
calls GPT-4o-mini. `extract_keywords(jd)` internally calls Mistral. The
orchestrator has no visibility into which provider ran.

**Alternatives rejected:**
- *Orchestrator with direct multi-provider awareness* — the orchestrator would
  need provider-specific error handling, retry logic, and prompt formatting for
  each model. This couples orchestration logic to provider implementation.
- *Single-provider orchestration* — loses the empirically grounded rationale
  for GPT as the critique model (observed harsher feedback).

**Load-bearing reason:**
The tool abstraction makes providers swappable. If GPT-4o-mini is replaced,
only the tool implementation changes — the orchestration loop is unchanged.
This is the same reason the Week 2 tool layer existed: to isolate the
orchestrator from implementation details of its dependencies.

**What this generalises to:**
The tool pattern is not specific to function calls or API integrations. Any
system that returns structured output can be a tool — including other AI
models. This is the insight that makes multi-model orchestration composable
at scale.

**Interview framing:**
"The architecture treats LLMs as tools. The orchestrator calls critique_cv()
the same way it calls get_spending_summary() in my finance agent. The provider
is an implementation detail of the tool, not a concern of the orchestrator.
That abstraction is what makes the system composable and the providers
swappable."

---

### D-03 — Model routing: three providers, three roles

**What was decided:**

| Provider  | Model             | Role                        | Justification                                     |
|-----------|-------------------|-----------------------------|---------------------------------------------------|
| Mistral   | mistral-small     | JD extraction + embeddings  | Existing integration; cheaper structured tasks    |
| Anthropic | claude-sonnet-4-6 | Orchestrator + drafter      | Complex multi-step reasoning; established Week 2  |
| OpenAI    | gpt-4o-mini       | Section critique            | Empirically harsher, more direct CV feedback      |
| Anthropic | claude-haiku-4-5  | Formatting validation       | Fast, cheap, sufficient for deterministic checks  |

**Load-bearing reason for the GPT critique choice:**
This is the only genuinely novel routing decision. The justification is
empirical: direct personal observation that ChatGPT gives harsher, more
actionable CV feedback than Claude — less tendency to flatter. Using GPT
here is defensible; using it arbitrarily to "demonstrate multi-provider"
would not be. The architecture should always be able to answer "why this
model for this task?" with something better than "to add variety."

**What this generalises to:**
Model routing decisions should be grounded in observed behavioural differences,
not capability marketing. In production systems, model assignment is a
first-class architectural decision with cost and quality implications.

**Interview framing:**
"Every model in this system has a specific role justified by observed behaviour,
not by arbitrary provider diversity. GPT-4o-mini critiques because I've
directly observed it gives harsher, less flattering feedback than Claude —
which is exactly what you want from a CV reviewer."

---

### D-04 — Dynamic scoring rubric

**What was decided:**
The scoring rubric is a versioned first-class object, not a static list.
Created by Mistral at the start from the JD; extendable by the orchestrator
during refinement when critique surfaces requirements not in the original JD.
Version number increments on each update.

**Alternatives rejected:**
- *Static rubric* — measures the CV against the JD as written, missing
  inferred requirements the critique model surfaces.
- *Unversioned mutable rubric* — makes it impossible to audit which scoring
  criteria applied to which draft iteration.

**Load-bearing reason:**
A dynamic rubric measures the CV against what the JD *means*, not just what
it says. Evaluation criteria are a design decision, not a given — in any
iterative refinement system, "are we measuring the right things?" should be
as live a question as "are we improving the thing we're measuring?"

**Safeguards agreed during architecture review:**
1. Maximum 2 rubric additions per iteration (prevents runaway requirement
   inflation that stalls keyword coverage artificially).
2. Each addition must be validated against the JD by the orchestrator before
   being accepted ("is this actually implied by the JD?"). Validation decision
   logged in audit trail.
3. Items added from critique tracked as `list[RubricAddition]` with fields:
   `keyword`, `added_in_iteration`, `triggered_by` (description of the
   critique item that surfaced it). Flat list is insufficient for audit.

**What this generalises to:**
Dynamic evaluation rubrics appear in any system where the evaluation criteria
and the thing being evaluated improve together. The pattern generalises to
benchmarks that evolve during model training, acceptance criteria that expand
during code review loops, and quality gates that adapt to discovered requirements.

**Interview framing:**
"The rubric isn't static. As the critique model surfaces requirements not
explicit in the JD, the orchestrator can extend the rubric — up to two additions
per iteration, with each validated against the JD. The rubric is versioned so
you can always trace which criteria applied to which draft."

---

### D-05 — Dual-signal convergence

**What was decided:**
The refinement loop terminates when both signals plateau: keyword coverage
score (0–1, proportion of rubric items present) AND critique score (0–10,
GPT's overall assessment). Both deltas must fall below threshold, or
max_iterations is reached, or the orchestrator declares a soft-stop.

Termination table:
```
keyword_delta < 0.05 AND critique_delta < 0.5  →  convergence
iteration == max_iterations                     →  hard stop
zero major critique items (last iteration)      →  soft-stop permitted
```

**Alternatives rejected:**
- *Single keyword score* — gameable: a CV can stuff keywords while remaining
  structurally weak, and the loop terminates on a false positive.
- *Single critique score* — fragile: the critique model could give inflated
  scores while required keywords are still missing.
- *Orchestrator-only termination* — "the model decides when to stop" without
  a grounded signal is a source of non-deterministic costs and is not testable.

**Load-bearing reason:**
Two orthogonal signals provide a more robust termination condition than one.
Neither signal can be satisfied by gaming the other. The soft-stop condition
(zero major items) gives the orchestrator a legitimate early-exit path without
reducing the termination condition to pure model judgment.

**Failure modes to watch during build:**
- Score inflation: critique score rises quickly in iteration 1 (early wins),
  then stalls. Loop runs to max_iterations on minor improvements.
- Rubric expansion stall: added requirements push keyword coverage down after
  rubric update, resetting the delta counter artificially.
- Conflicting signals: keyword coverage converges but critique score does not
  (or vice versa). The loop correctly continues — but may be frustrating if
  the human can see the CV is "good enough." The HITL phase is the escape valve.

**What this generalises to:**
Dual-signal convergence is a general pattern for any loop where a single
quality metric is insufficient. It appears in training loops (loss + validation
accuracy), search (precision + recall), and any iterative refinement where
multiple orthogonal dimensions of quality matter.

**Interview framing:**
"The loop uses two orthogonal convergence signals — keyword coverage and GPT
critique score. Either signal alone can be gamed or fail silently: a CV can
score well on keywords while being structurally weak, or receive high critique
scores while missing required terms. Both must plateau before the loop exits."

---

### D-06 — Audit trail separate from context

**What was decided:**
All orchestrator reasoning is logged to `run_log.jsonl` but never injected
back into the messages array. Context stays clean; the audit trail is complete
and inspectable after the run. Same pattern as Week 2 transcript logging.

**Load-bearing reason:**
Two independent concerns: (1) keeping the context window free of accumulated
reasoning verbosity, which would inflate token costs and potentially distort
future model behaviour; (2) producing an audit trail that is readable without
reconstructing the conversation. Separating them is the correct design.

**Schema refinement agreed during architecture review:**
`ReasoningEntry` should include `rubric_version: int | None` so that score
entries in the audit trail can be traced to the rubric that was active when
the score was computed. Without this, score progression in the output is
ambiguous if the rubric changed mid-loop.

**What this generalises to:**
Observability is a first-class concern in any agentic system. In production,
the ability to audit why an AI made a decision is often as important as the
decision itself. The pattern of separating context (what the model sees) from
audit trail (what the human reads afterwards) recurs in every serious deployment.

**Interview framing:**
"Reasoning is logged to the audit trail but never fed back into context — same
pattern as Week 2. This keeps the context window clean and prevents reasoning
verbosity from inflating token costs. The HTML output renders the full audit
trail as a collapsible reasoning trace: the CV is clean, but every decision
is inspectable."

---

### D-07 — Schema additions identified during architecture review

**What was decided:**
Four schema corrections agreed before build begins:

1. **`CritiqueItem` gains `applied: bool`** — distinguish "accepted in principle"
   from "actually reflected in the next draft." Log when accepted ≠ applied.

2. **`added_from_critique` in `ScoringRubric` becomes `list[RubricAddition]`**
   where `RubricAddition` has: `keyword: str`, `added_in_iteration: int`,
   `triggered_by: str` (description of critique item). Flat list loses
   provenance.

3. **`PipelineOutput` does NOT store `drafts: list[str]`** — intermediate drafts
   are checkpointed to disk as `draft_v0.md`, `draft_v1.md`, etc. Phase 6 reads
   them from `outputs/<run_id>/` to build the Changes tab diffs. `PipelineOutput`
   is a summary object; storing all draft text in it would make it a data warehouse.
   The checkpoint pattern handles this more consistently.

4. **`ReasoningEntry` gains `rubric_version: int | None`** — so score entries
   in the audit trail can be traced to the rubric that was active when scored.
   Without this, an iteration where the rubric expanded (dropping keyword coverage
   from 0.78 to 0.71 despite real improvement) looks like a regression in the
   Scores tab. One field prevents a misleading artefact in the output.

**Load-bearing reason:**
Schema gaps that are invisible at write-time become painful at read-time.
The audit trail provenance, the scoring ambiguity, and the Changes tab diffs
all require this state to exist — but the drafts should live on disk, not in
memory, consistent with the checkpoint pattern.

**What this teaches:**
Step 0 (schemas) is more important in this project than in Week 2 because
the schemas are communication contracts *between* providers, not just between
modules. The blast radius of a post-build schema change is larger.

---

### D-11 — Critique severity labels must be prompt-defined, not just schema-defined

**What was decided:**
`CritiqueItem.severity` uses two levels: `"major" | "minor"`. The definitions
are explicit in the GPT critique system prompt (not just in the schema comment):

- **major**: materially weakens the application or contradicts a JD requirement
- **minor**: improvement opportunity; the CV is acceptable without it

**Why this must be in the prompt, not just the schema:**
The soft-stop condition depends on "zero major items in the last critique." If
the GPT critique prompt doesn't define `major` with precision, the model will
calibrate severity on its own — inconsistently across iterations. A "major"
item in iteration 1 might be equivalent to a "minor" item in iteration 3. The
convergence condition then becomes meaningless.

**Three levels considered and rejected:**
`major | medium | minor` was considered. Rejected: the soft-stop condition
would need to specify whether "zero major items" or "zero major+medium items"
triggers soft-stop eligibility, adding ambiguity. Two levels with clear
definitions is simpler and sufficient.

**What this teaches:**
When a schema field drives control-flow decisions (here: loop termination),
the values of that field need specification that lives in the prompts, not
just the type annotations. Schema and prompt design are coupled.

**Interview framing:**
"The soft-stop condition depends on critique severity labels, so the labels
had to be defined precisely in the critique prompt — not just in the schema.
If the model calibrates severity on its own, the convergence condition becomes
iteration-dependent noise."

---

### D-08 — Cost tracking at model level, not provider level

**What was decided:**
`cost_breakdown` in `PipelineOutput` tracks cost at the model level:
`{"anthropic_sonnet": x, "anthropic_haiku": y, "openai_gpt4o_mini": z,
"mistral_small": w}` rather than at the provider level.

**Load-bearing reason:**
In demo mode, the Haiku orchestrator runs instead of Sonnet. A provider-level
breakdown collapses these into a single "anthropic" figure, making it
impossible to compare demo vs full mode costs meaningfully. Model-level
tracking makes the mode comparison honest and the cost cap logic cleaner.

---

### D-09 — `--dry-run` flag behaviour

**What was decided:**
Dry-run exits after Phase 1 (fit assessment). Outputs:
- Terminal summary: JD analysis, recommended CV, fit score, gaps
- `run_log.jsonl` written with phases 0 and 1 entries only
- No `cv_final.html` or `cv_final.md` produced (dry-run is explicitly
  pre-draft)

No HITL checkpoint in dry-run — the terminal output IS the result.

**Load-bearing reason:**
Dry-run is useful for validating corpus ingestion and JD parsing before
committing to a full API spend. It should be cheap to run and immediately
readable. If it produced partial HTML output, the user would have to open a
file to see results — defeating the purpose.

---

### D-10 — ChromaDB de-duplication on re-ingestion

**What was decided:**
De-duplication key is `filename + version_date` from `CVMetadata`. On
re-ingestion without `--replace`, files with a matching key are skipped with
a warning. With `--replace`, the existing document is deleted and re-ingested.
Duplicate check runs before any embedding calls to avoid unnecessary API spend.

**Load-bearing reason:**
Silent duplicates in ChromaDB skew retrieval scores — the same CV appears
twice with slightly different embedding noise, and one of them wins the
retrieval even if neither is the best match. The de-duplication key must be
stable across re-runs (filename alone is sufficient for most cases, but
version_date catches intentional updates to the same file).

---

## Findings Log (populated during build)

*Entries added here when build reveals something that changes or confirms
an architectural decision. Format: what was found, which decision it affects,
what changed (if anything).*

---

### F-01 — Step 0: package is `tailor/`, not `orchestrator/` (build-prompt vs spec discrepancy)

**What was found:** The build-session opening prompt referred to the package as
`orchestrator/orchestrator.py` and `orchestrator/__main__.py` in its Step 8.
SPEC §7 (directory tree), §8, and every CLI example (`python -m tailor run`)
use `tailor/`. The spec is the source of truth.

**Decision affected:** none re-opened — this is a naming reconciliation, not a
design change. **What changed:** the package is `tailor/`; the main orchestration
loop lives in `tailor/run.py` (per §7's tree, which is more authoritative than
§8's prose mention of `tailor/tailor.py`). Recorded so the discrepancy isn't
re-litigated mid-build.

---

### F-02 — Step 0: generic type-hint-driven deserialiser instead of per-class `from_dict`

**What was found:** The 16 schemas nest deeply (`PipelineOutput` → `FitAssessment`
→ `dict[str, SectionRecommendation]`; `IterationScore` → `dict[str, SectionScore]`;
`ScoringRubric` → `list[RubricAddition]`). Hand-writing `from_dict` per class
would be 16× the boilerplate and 16× the places to get a nested reconstruction
wrong.

**Decision affected:** D-07 (schemas are cross-provider contracts; round-trips
must be correct). **What changed (implementation, not architecture):** a single
`Serializable` mixin provides `to_dict`/`to_json`/`from_dict`/`from_json`.
`from_dict` reads the dataclass's resolved type hints and recursively coerces
JSON values — handling nested dataclasses, `list[X]`, `dict[str, X]`, and
`X | None`. Unknown keys are ignored (forward-compatible reads); missing required
fields raise `TypeError` (asserted in tests). 49 tests cover round-trips for
every schema plus the D-07/D-11 correction guards.

---

### D-23 — Seniority is a soft ranking preference, not a hard pre-filter (to confirm at Step 3)

**What was decided (provisional; confirm against real JDs when retrieval is built):**
Phase 1's seniority signal **ranks** candidate CVs but never **excludes** them.
SPEC §3.8 calls seniority a "pre-filter"; this refines that to a soft, band-based
preference rather than a hard gate — encoded in `config.yaml` under `retrieval`.

**Load-bearing reason (user/Claude feedback during Step 1 sidecar authoring):**
Application Engineer / Deployment Specialist roles at AI-native companies often
carry no clean seniority signal in the JD — titled "Senior" or "Principal" but
scoped at what would be director-equivalent elsewhere. A hard seniority filter
would wrongly drop a strong generic CV. This is most acute for `cv_type: generic`
CVs, whose `target_role` deliberately spans levels (the corpus has three such CVs,
all `seniority: director` but written to suit principal→VP-equivalent roles).

**Mechanism (provisional):** a `seniority_order` ladder
(`senior < principal < director < vp`) yields a band distance; within ±1 step is
treated as a full match, beyond that a graded penalty — applied to ranking only.
`seniority_filter_mode` is `soft` for both cv_types initially; tighten
`job_specific` to a harder filter only if real runs show false positives.

**Links:** depends on [[F-06]] (single scalar seniority) and the canonical
vocabulary in D-22. Revisit in Step 3 (Phase 1 fit assessment) — added to Open
Questions.

---

### F-08 — Cost figures are list-price ESTIMATES, not actual billing (Mistral runs free-tier)

**What was found:** The ingestion "cost" (≈$0.0011) is computed in code as
`tokens / 1e6 × list_price`, not read from any billing API. The Mistral account
is on the free "Experiment" tier (rate-limited, no payment method) so actual
spend is **£0**; the estimate overstates it by assuming the paid rate.

**Affects D-08 (cost tracking).** When the per-model `cost_breakdown` and the
`run_complete` footer are built (Step 8/9), label them as **estimated**
(list-price), e.g. `estimated_usd`, and state the assumed per-model rates in one
place. The portfolio value is "what this would cost at scale on paid tiers" — but
it must never read as a real invoice. Anthropic/OpenAI calls in later steps *are*
paid (those keys are on paid accounts), so the estimate matters there; Mistral
stays free.

---

### F-07 — Step 1: mistralai 2.4.9 puts the SDK under `mistralai.client` (RAG import path still valid)

**What was found:** The installed `mistralai==2.4.9` has **no top-level
`__init__.py`** — `import mistralai` yields an empty namespace package
(`__file__ is None`). The real SDK lives under `mistralai.client`: the client is
`from mistralai.client import Mistral`, the base error is
`mistralai.client.errors.MistralError` (with `SDKError`, `NoResponseError`,
`ResponseValidationError` beneath it), and HTTP status/headers are on
`exc.raw_response` (an `httpx.Response`), not on the exception directly.
Embeddings: `client.embeddings.create(model="mistral-embed", inputs=[...])` →
`resp.data[i].embedding`, with token usage on `resp.usage`.

**Why it matters / what changed:** This is the same import path the Week 1 RAG
helper used (`from mistralai.client import Mistral`), so that reuse holds — but
the RAG retry logic keyed on a flat `MistralError.status_code`/`.headers`, which
in 2.4.9 must be read from `exc.raw_response`. `tailor/helpers.py`'s
`call_with_retry` reads status/headers defensively across SDK shapes
(`raw_response` first, then a flat `status_code`/`headers`) so the same wrapper
will also cover the Anthropic/OpenAI clients added later. `requirements.txt`
pinned `mistralai>=2.0.0,<3.0.0` to match. Verified by introspection, no API call.

---

### F-06 — Step 1: sidecar metadata uses single scalar values, validated at write-time

**What was found:** The first hand-filled sidecar (Adtech Consulting, a *generic*
CV) packed ranges into single fields: `seniority: principal, director, VP`,
`target_role` as a comma-list, and a `target_company` despite `cv_type: generic`.
`seniority: principal, director, VP` is valid YAML but parses as the *string*
`"principal, director, VP"` — it silently would not match any seniority filter.

**Decision (D-22):** Sidecar filter fields (`cv_type`, `target_role`,
`seniority`, `target_company`) are **single scalar values**, because ChromaDB
metadata is scalar — lists can't be stored or filtered. A generic CV's breadth
is carried by its embedded content and by semantic retrieval, not by cramming
multiple values into a filter field. Controlled vocabularies: `cv_type ∈
{generic, job_specific}`, `seniority ∈ {senior, principal, director, vp}` (added
`vp`). `target_company` is `null` for generic CVs. `skills_emphasis` is the one
list field.

**What changed:** Added `validate_sidecar(data) -> (errors, warnings)` to
`corpus/metadata.py`, called by `load_sidecar` (raises on errors). This applies
R-09 (validate structured input at write-time, not downstream) to *human*-authored
input — and matters more here because the user will batch-generate the remaining
sidecars with an LLM: the validator turns a silent retrieval-time mismatch into
an immediate, fixable error. A generic-CV-with-company is a warning (surfaced at
ingest), not a hard error.

---

### F-04 — Step 1: the CV corpus has NO heading-style structure — D-15's parse assumption is wrong; reuse the RAG table-aware loader

**What was found:** The 7 real CVs (`data/cvs/`) are **table-based**: each is a
single table, all body content lives in its cells, and `python-docx`'s
`doc.paragraphs` sees only 1–2 top-level `Normal` paragraphs. A heading-style
parser (D-15) would have produced a near-empty corpus **without crashing** —
exactly the R-01 silent-partial-parse failure mode. Even inside the table,
hierarchy is not reliably heading-styled: section headers are mostly
`Heading 1`/16pt but "Core Skills" is `Heading 4`/14pt, and "AI Projects" is
`Heading 1`/16pt in some CVs and `Heading 4`/14pt in others. Company names
collide with that 14pt band (`Heading 3`, `Heading 4`, and `Normal`-bold all
appear at 14pt).

**Decision affected:** **D-15 (heading-style parsing) is corrected.** Also the
SPEC §3.8 canonical section list (see F-05).

**What changed:**
1. **Reused the Week 1 RAG `docx_loader.py`** (`corpus/docx_loader.py`) rather
   than reinventing. Its table walk (read *every* cell, pair the date column),
   `numPr` bullet detection, and rendered-size resolution (run → style →
   base-style chain) are exactly what this corpus needs — it is the same family
   of documents that logic was built for. Simplified: dropped the PDF/format-
   agnostic split (cv-tailor ingests only `.docx`) and the `source_format` field.
2. **Parsing is now fingerprint-based, not heading-based** (the RAG R-10
   "discover structure, don't assume it" pattern). The robust section-boundary
   signal is **text matched against a canonical-section vocabulary**, not style
   or size — because style/size are inconsistent but the section *titles* are a
   small known set.

**What this teaches (portfolio):** The single most valuable thing Step 1 did was
*look at the data before writing the parser*. The R-01 note predicted this exact
failure; running a 30-line discovery dump in the container turned a predicted
risk into an observed fact and saved a silently-wrong corpus.

---

### F-05 — Step 1: section model grounded in the observed corpus (vocabulary + size split; two new canonical sections)

**What was found (the corpus fingerprint):**
- Body text 11pt; name "Michel Guillon" 18pt-bold; contact lines 10pt (above
  "Profile").
- Section headers match a known vocabulary: Profile, Core Skills, Work
  experience, (Technical &) AI Projects, Education, **Languages**, Interests.
- Inside Work experience, **company = 14pt** (any style), **role line = ≤12pt**
  (carries a date, inline or in the date column), **bullet = `numPr`**. A company
  can hold several roles (Imagination Technologies has 3); bullets attach to the
  company block, not cleanly to individual roles.

**Decisions made (refining SPEC §3.8, recorded before writing the sectioniser):**

- **D-19 — Section detection = canonical-name vocabulary + size split.** A
  paragraph is a section header iff its normalised text matches a canonical
  alias *and* it is non-bullet and visually elevated (size > body OR
  Heading-styled OR bold). Within the experience block only, a new company
  sub-section starts at each non-bullet paragraph at the block's max non-bullet
  size (14pt here); role lines (≤12pt) and bullets are that company's content.
  Aliases live in `config.yaml` (`section_aliases`) — the discovered vocabulary,
  persisted, never re-guessed at runtime (R-10).

- **D-20 — Two canonical sections added: `header` and `languages`.** The spec's
  list started at `profile`, but every CV has a name/contact block above it and
  a Languages section below. `header` (position 0) and `languages` are added,
  both **static**. `certifications` stays in the vocabulary though absent from
  this corpus (present-only assembly handles its absence). Static set for this
  corpus: `{header, education, languages, certifications, interests}`; active
  (critiqued): `{profile, skills, experience_<company>, ai_projects}`.

- **D-21 — Experience sub-sections are per company AND per role-group.**
  *(First proposed as per-company; revised after user feedback — roles are worked
  on as distinct sections across the LLMs, so they should be separate.)* SPEC §3.8
  said "one CVSection per job per company". Naive per-job splitting fails on this
  corpus because companies stack promotions before shared bullets (Appnexus:
  Director → Associate Director → 4 shared bullets; Imagination: Senior Customer
  Engineer → Application Specialist → 4 shared bullets) — splitting those orphans
  the bullets. Resolution: split on **role-group** boundaries — a new section
  starts at a role line that *follows a bullet*; consecutive role lines with no
  bullet between them stay together. This gives per-role granularity while
  keeping promotion-stacks intact. `section_id =
  experience_<company>_<first-role-slug>` (e.g.
  `experience_appnexus_xandr_director_solution_consulting`). Observed result: the
  AI CV goes from 4 company sections to 7 role-group sections. Cost rises modestly
  but is bounded — sections freeze once converged (D-12), so critique stays focused.

---

### F-03 — Step 0: Docker is the run target; image pins Python 3.13-slim (supersedes the 3.12 note)

**What was found:** Mid-Step-0, the spec was extended (§6, §7, §7.5) to make
**Docker the deployment target** for the M720q homeserver. §6 now routes every
CLI command — including `pytest tests/` — through `docker compose run --rm cli`.
The `Dockerfile` pins `python:3.13-slim`.

**Decisions affected:** none re-opened, but two consequences recorded:
1. **Python version:** the container is **3.13**, not 3.12. Local dev here is
   3.13.7, so dev and prod now match. The "Python 3.12" line in earlier docs is
   superseded by the Dockerfile. (The schema code uses 3.10+ `X | None` unions and
   `typing.get_type_hints`, both fine on 3.13.)
2. **Verification command:** Step 0's gate is now run as
   `docker compose run --rm cli pytest tests/`. Verified: image builds cleanly on
   3.13-slim (requirements.txt resolves with no conflicts — chromadb 1.5.9,
   anthropic 0.105.2, openai 2.40.0, mistralai 2.4.9, pytest 9.0.3; `tenacity`
   pulled in transitively, useful for `call_with_retry` later), and 49 tests pass
   in-container.

**What changed (scaffolding):** added `Dockerfile`, `docker-compose.yml`
(cli + backend services sharing one image), `docker-compose.prod.yml` (backend
overlay), `.dockerignore`, and `ADAPTING.md`. The `frontend` service and prod
multi-stage build are defined in SPEC §7.5 but deferred to the UI phase and
gated behind comments so `docker compose build` doesn't fail on the
not-yet-existing `frontend/Dockerfile.dev`. The SSE `proxy_buffering off` nginx
note (from the RFI project) is captured as a comment in the prod overlay so it
isn't lost before the UI build.

---

## Cost Tracking (populated during build)

| Run | Mode | Mistral | Anthropic Sonnet | Anthropic Haiku | OpenAI | Total USD |
|-----|------|---------|-----------------|-----------------|--------|-----------|
| — | — | — | — | — | — | — |

---

## Test Coverage Notes (populated during build)

*Which behaviours are tested deterministically (pytest), which require LLM-gated
tests, and which are tested by inspection only.*

---

## Open Questions (resolved before closing the project)

- [ ] Does the convergence threshold (keyword_delta < 0.05, critique_delta < 0.5) need
      calibration after seeing real iteration data? Document the first real run's
      score progression to validate.
- [ ] Is `mistral-small` the right model for Phase 0, or does structured extraction
      quality warrant `mistral-medium`? Test on 3 real JDs before committing.
- [ ] **Experience budget granularity (Phase 2/3):** `budgets.yaml` derives one
      budget per `section_type`, but per-role-group experience sections vary widely
      (observed 23–187 words: a terse early role vs a detailed recent one). A single
      `experience` target (108) over-inflates small role sections. Decide at drafting
      time whether to bucket experience budgets (e.g. by seniority/recency) or treat
      max_words as a ceiling only rather than target. Surfaced by D-21's revision.
- [ ] **D-23 — seniority soft filter:** confirm the soft/band-based seniority
      ranking (not hard pre-filter) against real Application Engineer / Deployment
      Specialist JDs when Phase 1 retrieval is built. Verify a generic
      `seniority: director` CV is not excluded by a JD titled "Senior". Tune the
      band width and decide whether `job_specific` should tighten.
---

## Reuse Analysis — What the RAG and RFI Projects Teach This Build

*Synthesised from LEARNING_NOTES.md (Week 1 RAG pipeline) and LEARNING_NOTES_RFI.md (RFI Answer Builder) before the Week 3 build began. Each entry maps a prior finding to a concrete implication for the orchestrator.*

---

### R-01 — docx parsing: heading styles are not guaranteed, and silent failures are the dangerous kind

**From:** RAG LEARNING_NOTES Phase 2 ("real documents lie about their structure")

**What was found:** A Word document's visual hierarchy and its underlying markup are two different things. The RAG project's CV used proper heading styles for sections but direct formatting (font size) for company names — a naive style-only parser missed them silently, producing wrong-but-plausible chunks with no error.

The fix was a fingerprint profiler that discovers structure rather than assumes it. But the key lesson for this project is the failure mode: **the pipeline ran, produced output, and was wrong. Nothing crashed.**

**Implication for the orchestrator — Section boundary detection:**
The orchestrator's ingestion step must not assume heading styles are present or consistent across all 6 CV .docx files. Ingestion must verify section extraction output explicitly: after parsing, print a section inventory (`section_id: N words`) and require human confirmation before proceeding. A CV that parsed to 2 sections instead of 8 must fail loudly, not silently produce a 2-section corpus.

Specifically from the RAG notes: "company names aren't heading-styled at all, and one company shares the exact style used for job titles." This is a CV-specific risk for exactly the kind of corpus this project ingests. The ingestion verification step is load-bearing, not ceremonial.

**Concrete action:** Step 1 (corpus ingestion) must include an explicit section extraction verification pass: show the section inventory parsed from each CV file, warn if any section count is below a minimum threshold (e.g. < 4 sections on a 2-page CV is almost certainly a parsing failure), and block ingestion until a human confirms.

---

### R-02 — docx parsing: the Paragraph model needs content-derived fields, not just formatting fields

**From:** RAG LEARNING_NOTES Phase 2 ("The common paragraph model: pluggable earns its keep")

**What was found:** The RAG project added `date` and `override` fields to the `Paragraph` dataclass during the Phase 2 architecture conversation — not at the start. Without them, the docx loader would have lost capability or needed parallel data structures. The architecture conversation that surfaced this was called "the single highest-leverage decision in Phase 2."

**Implication for the orchestrator — CVSection word/line count fields:**
`CVSection.word_count` and `line_count` are exactly this pattern: content-derived fields that must be on the intermediate model, not computed separately later. They are needed for `SectionBudget` derivation (Step 1), for length constraint enforcement in the critique prompt (Phase 3), and for the Phase 5 assembled-length check. If they're not on `CVSection` from ingestion, every downstream component that needs them has to either reparse the file or carry a side-channel.

This validates the schema decision to put `word_count` and `line_count` directly on `CVSection` rather than computing them at critique time.

---

### R-03 — ChromaDB: distance metric is set at collection creation and is immutable

**From:** RFI LEARNING_NOTES entry 9 ("Four collections, one per strategy × distance metric")

**What was found:** `collection.get_or_create()` with `metadata={"hnsw:space": "cosine"}` sets the metric permanently. You cannot switch metric at query time — if you create a collection with L2 and query it expecting cosine, you get wrong rankings with no error.

**Implication for the orchestrator — collection naming discipline:**
The orchestrator creates one ChromaDB collection for the CV section corpus. The collection must be created with the metric explicit in the code and in the collection name (`cv_sections_cosine`). If the collection already exists with a different metric (from an earlier failed setup), `get_or_create` will silently use the old metric. The ingestion step must check that the existing collection's metric matches the configured one, or delete and recreate.

**Concrete action:** In `corpus/ingest.py`, after `get_or_create`, verify `collection.metadata["hnsw:space"] == config.metric`. If it doesn't match, raise with a clear message: "Collection exists with metric X, config requires Y. Run with --replace to recreate."

---

### R-04 — ChromaDB: metadata cannot contain None or empty strings

**From:** RFI LEARNING_NOTES entry 9 ("Metadata sanitisation")

**What was found:** ChromaDB doesn't accept None or empty strings in metadata in some versions. The RFI project stripped them before `collection.add()`. The semantic loss ("this section has no target_company") is preserved by the absence of the key rather than a sentinel value — filtered retrieval still works for sections that do have a target_company.

**Implication for the orchestrator:**
`CVMetadata.target_company` is `str | None`. Before adding any section document to ChromaDB, the ingester must sanitise metadata: omit None-valued keys rather than passing None. Same for any `str` field that might be empty. Add a `sanitise_metadata(d: dict) -> dict` helper to `corpus/ingest.py` that strips None and empty-string values.

---

### R-05 — call_with_retry() is not optional: the embedding API will rate-limit on a real corpus

**From:** RFI LEARNING_NOTES entry 9 ("Four 429 rate-limits during the Strategy B L2 collection; per-collection checkpoint would have lost all progress for that collection")

**What was found:** The RFI project hit 4 rate-limit errors mid-ingestion on a 544-chunk corpus. `call_with_retry()` absorbed them invisibly. Without it, the run would have crashed at chunk ~300 and required full re-ingestion.

**Implication for the orchestrator:**
The CV corpus is smaller (~50-100 sections across 6 CVs) but the same risk applies. More importantly, the refinement loop makes multiple sequential API calls across three providers. A transient 429 from GPT-4o-mini mid-loop without retry would abort the run and lose all iteration state. `call_with_retry()` must wrap every API call from every provider — this is already in the spec but deserves emphasis: it is not a polish item, it is load-bearing from the first real run.

---

### R-06 — Checkpointing granularity: the unit of resumable work is the natural failure unit

**From:** RFI LEARNING_NOTES entry 9 ("Per-file checkpointing, not per-batch and not per-collection") and RAG LEARNING_NOTES Phase 6 ("A long batch job against a rate-limited API MUST checkpoint")

**What was found:** RAG lost 91 completed API calls because the stress test only checkpointed at the end. RFI chose per-(collection, file) checkpointing — the natural unit of recoverable work — over per-batch (too chatty) or per-collection (too coarse, loses too much on failure).

**Implication for the orchestrator:**
The checkpoint unit for the ingestion step is one (section_id) — write to ChromaDB and save the checkpoint after each section is embedded. For the refinement loop, the checkpoint unit is one complete iteration — all section files for iteration N written to disk before iteration N+1 begins. A crash mid-loop loses at most one iteration of work, not the whole run.

This is already in the spec's checkpoint pattern, but the RFI experience makes the granularity choice concrete: per-section for ingestion, per-iteration for the loop.

---

### R-07 — Semantic retrieval beat hybrid on a small, paraphrase-rich corpus — relevant calibration

**From:** RFI LEARNING_NOTES entry 13 ("Counter to the spec's intuition, hybrid does NOT beat semantic on this corpus")

**What was found:** The RFI eval matrix showed semantic retrieval beating hybrid (BM25 + semantic) on a 280–540 chunk corpus where test questions closely paraphrased corpus content. BM25 occasionally promoted high-token-overlap chunks that weren't topically relevant, and RRF's contribution was small when semantic alone was near-saturated. Likely to reverse on larger or more terminology-heavy corpora.

**Implication for the orchestrator:**
The CV section corpus (~50-100 sections, 6 CVs) is even smaller and more paraphrase-rich than the RFI corpus. Phase 1 (fit assessment) uses Mistral embeddings for retrieval. The spec currently uses pure semantic retrieval — this is almost certainly correct for this corpus size. Do not add BM25 hybrid complexity to the Phase 1 retrieval. If the corpus grows substantially (20+ CVs, 200+ sections), revisit.

Documenting this explicitly: the RAG project's "tune when it earns its keep at scale" finding + the RFI project's empirical confirmation both point the same direction.

---

### R-08 — LLM-as-judge over-scores; actionable signal lives in retrieval metrics and edge cases

**From:** RFI LEARNING_NOTES entry 13 ("LLM-judge over-scores. Real signal lives in retrieval-gap and completeness")

**What was found:** Faithfulness = 5.00 and Relevance = 5.00 across all 36 RFI configurations. The judge was consistently too generous on absolute scoring. The actionable metrics were retrieval-gap rate and completeness, which showed real variation. A judge that allows gradations only at the top cannot discriminate good from very good.

**Implication for the orchestrator — critique scoring:**
GPT-4o-mini's `overall_score` (0–10) in the `Critique` schema is playing a role analogous to the LLM-as-judge in the RFI project. If GPT consistently scores drafts at 8.5+ from iteration 1, the critique score will fail to drive the convergence signal. The critique prompt must define the scoring rubric explicitly with anchors: "a 9 requires X, Y, and Z; a 7 means one section still has a major issue; a 5 means multiple structural problems remain." Without anchors, the score will reflect GPT's training priors, not the actual draft quality relative to this JD.

This is a concrete implication for the Step 5 (critique tool) prompt design — it needs explicit score anchors, not just "rate this CV from 0-10."

---

### R-09 — Validate LLM-produced structured output before it touches anything downstream

**From:** RAG LEARNING_NOTES Phase 2 ("When the LLM won't honour your grammar") and RFI LEARNING_NOTES entry 3–4 ("Validate BEFORE showing to human")

**What was found (RAG):** Mistral produced compound `&&` signals on three consecutive iterations despite explicit "FORBIDDEN" language in the prompt. The no-`eval()` parser that rejected them before `chunker.py` ever saw the config was the load-bearing defence. Validating at config-WRITE time (not chunk time) meant the failure happened while the human was watching, not three steps later.

**What was found (RFI):** The validator runs between LLM output and human review — a broken mapping (e.g. same column assigned two roles) is rejected mechanically before the human sees the proposal. Human attention is reserved for semantic correctness only.

**Implication for the orchestrator — structured outputs from Mistral and GPT:**
Phase 0 (Mistral JD extraction → `JDAnalysis` + `ScoringRubric`) and Phase 3 (GPT critique → `Critique`) both produce structured output that downstream phases depend on. Both must be validated against their schemas before use:
- `JDAnalysis`: required fields present, `required_keywords` non-empty, `seniority_level` in known set
- `Critique`: `overall_score` in 0-10, all `CritiqueItem.severity` values in `{"major", "minor"}`, `section` references valid section_ids

Validation failures should be retried once (the LLM may have hallucinated a field name), then surfaced to the human if the retry also fails. Never let a partially-valid `Critique` enter the accept/reject loop — a missing `severity` field would silently bypass the soft-stop condition.

---

### R-10 — The "discover + persist" pattern applies to section boundaries, not just column mappings

**From:** RFI LEARNING_NOTES entry 2 ("Discover schema, don't assume it") and entry 6 ("Persist discovery output, detect section markers")

**What was found:** The RFI profiler discovers per-file schema (sheet, header row, column mapping) and persists it to a config file. The loader treats the config as ground truth — it never re-runs discovery at load time. Discovery is expensive and human-validated; runtime is deterministic and config-driven.

**Implication for the orchestrator — section boundary discovery:**
The ingestion step's section boundary detection (parsing heading styles from .docx files) is the equivalent of the RFI profiler's column mapping discovery. The result — "this CV has these sections in this order" — must be persisted to the `CVSection` metadata and stored in ChromaDB, not re-derived on each tailoring run. If the section structure of a CV changes (rare, but possible if you add a new section), re-ingest with `--replace`. The runtime (tailoring) path treats ChromaDB metadata as ground truth.

This also means the YAML front-matter written per section file during ingestion is load-bearing — it is the persisted discovery output.

---

### Summary: what changes, what is confirmed, what is new

**Confirmed as-is in the spec:**
- Semantic-only retrieval for Phase 1 (R-07)
- `call_with_retry()` wrapping all API calls (R-05)
- Per-section + per-iteration checkpointing (R-06)
- Schema validation before downstream use (R-09)

**Requires concrete action before Step 1:**
- Ingestion verification pass: section inventory + human confirmation gate (R-01)
- ChromaDB collection metric verification on `get_or_create` (R-03)
- Metadata sanitisation helper (strip None and empty strings) in `corpus/ingest.py` (R-04)

**Requires prompt design decision before Step 5:**
- Critique `overall_score` must have explicit anchors (e.g. 9 = X, 7 = Y, 5 = Z) to avoid the LLM-judge over-scoring problem (R-08)

**Validates schema decisions already made:**
- `CVSection.word_count` and `line_count` as fields on the intermediate model, not computed later (R-02)
- Section discovery persisted to ChromaDB metadata, not re-derived at runtime (R-10): the unit of work is a section, not a CV

**What was decided:**
The refinement loop operates at section granularity. Each section is drafted,
critiqued, revised, and converged independently. Sections that converge early
are frozen — excluded from subsequent critique calls. The final CV is assembled
from the best-accepted version of each section, ordered by `CVSection.position`.

**Alternatives rejected:**
- *Monolithic CV drafting* — treating the CV as a single document means a strong
  profile and a weak experience section share the same iteration count. The loop
  can't stop working on the profile just because it's done.
- *Section-level drafting, CV-level critique* — sending the full CV to GPT for
  critique but revising sections individually. Rejected because critique items
  would still be scoped to sections, making the full-CV send unnecessary tokens.

**Load-bearing reason:**
Different sections converge at different rates. The profile might be excellent
after one pass; a specific experience section might need three. Freezing converged
sections makes each subsequent iteration cheaper and the critique more focused.
By iteration 3, the system is often only critiquing one or two sections.

**The real-world observation that drove this:**
The user sometimes prefers an earlier version of a section over the final one —
e.g. profile v2 might be sharper than profile v3 if the final revision over-optimised.
Section-level storage makes this recoverable: Phase 4 HITL can show section
version history and let the human choose.

**What this generalises to:**
Decomposing a document into independently refinable units is a general pattern
for any iterative generation task with heterogeneous quality across parts. The
unit of convergence should match the unit of quality variance — not the unit of
output format.

**Interview framing:**
"The refinement loop works at section granularity, not whole-CV. Sections that
converge early get frozen, which makes each subsequent iteration cheaper and
more focused. The final CV is assembled from the best version of each section —
and because intermediate versions are checkpointed, the human can choose an
earlier version of any section if a later revision over-optimised."

---

### D-13 — Static sections: presence in assembly, invisible to the loop

**What was decided:**
`CVSection.static: bool` marks sections that are copied verbatim from the base CV
and never sent to the critique loop. `interests` is always static. `education`
and `certifications` are typically static. Static sections appear in the
assembled CV at their correct `position` but generate no `CritiqueItem` entries,
no `SectionScore` entries, and no version files — only a single `_static.md`
checkpoint.

**Load-bearing reason:**
Including static sections in the critique loop wastes GPT tokens on content
that won't change and can't meaningfully improve. Marking them explicitly also
makes the HITL display cleaner — the human can see at a glance which sections
were worked on vs. carried over unchanged.

**Interesting edge case:**
`interests` is not just static — it's a proof that the `static` flag is about
editorial intent, not section importance. Interests won't change between
tailoring runs for the same person, but it needs to appear in the right position
in the assembled CV. Static ≠ absent.

---

### D-14 — Length budget derived at ingestion from observed corpus behaviour

**What was decided:**
Section length constraints are not hardcoded. After ingesting all CVs, the
ingestion script computes a `SectionBudget` per `section_type` from observed
word counts: `min_words`, `max_words`, `target_words` (median). Written to
`budgets.yaml`. The total word count across all section targets is the
two-page envelope, derived from the user's actual CV corpus.

The critique prompt uses `target_words` as the drafting target and flags:
- `major` if a section exceeds `max_words` (breaks the two-page constraint)
- `minor` if a section is materially below `min_words` (undertells the role)

Phase 5 (Haiku) does a final assembled-length check before output.

**Alternatives rejected:**
- *Hardcoded word limits per section* — requires the user to estimate limits
  before seeing how the system behaves. Error-prone and not portable.
- *No length constraint* — the two-page constraint is real and non-negotiable.
  Without it, the system would produce excellent-but-unsubmittable CVs.

**Load-bearing reason:**
The best source of truth for "how long should this section be" is the user's
own existing CVs. They've already made these tradeoffs when writing them.
Measuring from the corpus respects those decisions rather than imposing external
constraints.

**Portfolio/adapting note:**
Documented in `ADAPTING.md`: if you are adapting this project for your own CV
corpus, run ingestion first to derive budgets before any tailoring runs. The
`budgets.yaml` output shows you exactly what the system inferred about your
section lengths.

**What this teaches:**
Constraints that matter to the user (two-page CV) should be captured as
measurable invariants and enforced throughout the loop — not just checked at
the end. Making the constraint a `CritiqueItem` means it competes on equal
footing with content improvements: the orchestrator can decide to prioritise
length compliance over a marginal wording improvement.

---

### D-15 — CVs are .docx files; ingestion uses python-docx with heading-style parsing

> **⚠ CORRECTED during Step 1 — see F-04/F-05.** The real corpus has no reliable
> heading-style structure (table-based; section headers span Heading 1/3/4 and
> 16/14pt; companies collide with section headers at 14pt). Section detection is
> now **canonical-name vocabulary matching + a size-based company split inside
> the experience block** (D-19), using the reused RAG table-aware `docx_loader`.
> The heading-style assumption below is retained for the record but superseded.

**What was decided:**
All source CVs are `.docx` files. The ingestion parser uses `python-docx` to
extract section text and measure word/line counts. Section boundary detection
uses heading styles (Heading 1 / Heading 2), not line splitting or regex
patterns. YAML front-matter is written to companion `.yaml` files alongside
the extracted section markdown files.

**Why heading-style parsing, not line splitting:**
Learned from the Week 1 RAG project: `.docx` files with consistent heading
styles parse cleanly with `python-docx`; files that use manual formatting
(bold text, font size changes) instead of styles require fallback heuristics
that are fragile. The CV corpus uses consistent heading styles — this is a
precondition for ingestion to work reliably, and should be documented in
`ADAPTING.md`.

**Adapting note:**
If adapting this project for your own CV corpus: ensure your .docx files use
Word heading styles (Heading 1 for section titles) rather than manually
formatted text. The ingestion parser will fail silently on manually formatted
headings — verify section extraction output before running tailoring.

**What this generalises to:**
Document parsing quality is a function of document authoring discipline.
Any system that ingests structured documents should specify the authoring
conventions it depends on, and verify them at ingestion time rather than
assuming them.

---

### D-16 — Fit assessment has three outcomes; no_fit stops the pipeline

**What was decided:**
Phase 1 produces one of three outcomes: `strong`, `partial`, or `no_fit`.
`no_fit` terminates the pipeline immediately — no drafting, no API spend
beyond Phase 1. The `no_fit_reason` field provides a plain-English explanation.

Gap types and their pipeline implications:
- `keyword` — fixable by tailoring; never triggers no_fit
- `experience` — partially addressable; triggers partial at worst
- `hard_requirement` — not fixable (missing credential, clearance, certification); triggers no_fit
- `seniority` — judgment call; triggers no_fit only on severe mismatch

The human can override no_fit and proceed anyway — the system is honest, not gatekeeping.

**Load-bearing reason:**
A fit assessment that always proceeds is less valuable than one that can say
"don't apply." The most useful output of the pipeline is sometimes "this JD
has a non-negotiable requirement you don't meet." Saving token spend and
application time is a feature. Making the stop path explicit (typed outcome,
plain-English reason, override option) is better than letting the pipeline
produce a confidently tailored CV for a role the candidate can't fill.

**What this teaches:**
Pipeline early-exit is a first-class outcome, not an error state. Any system
that processes input should have an explicit "this input is out of scope"
path. Explicit is better than letting a downstream stage produce wrong output.

**Interview framing:**
"The fit assessment can stop the pipeline entirely if it finds a blocking gap.
That saves time and API spend, and it's honest. The human can override if they
want to apply for a stretch role, but the system won't pretend a gap doesn't exist."

---

### D-17 — Phase 1 recommends a section-level mix, not a single base CV

**What was decided:**
`FitAssessment.recommended_sections` is `dict[str, SectionRecommendation]` —
the best source section for each section_type drawn from across the full CV
corpus. `FitGap` replaces the flat `skills_gaps: list[str]` with a typed
structure: `gap_type`, `addressable`, `severity`, `reason`.

**Load-bearing reason:**
With section-granular ingestion, different CVs may have the strongest version
of different sections. Recommending a single CV from a section-level corpus
ignores retrieval capability that's already built. The recommendation granularity
should match the retrieval granularity.

**The HITL consequence:**
The Phase 1 checkpoint shows a section mix table. The human can override
individual section sources. This is more useful than yes/no on a single CV,
and the conversational HITL (D-18) handles the override naturally.

**What this teaches:**
The retrieval granularity and recommendation granularity should always match.
If you've built section-level retrieval, the recommendation should be at section
level. Anything coarser leaves retrieved capability unused.

---

### D-18 — Conversational HITL: natural language interpreted into structured pipeline decisions

**What was decided:**
Each HITL checkpoint is a conversational exchange with explicit options plus
a free-text escape hatch. A Claude Haiku call interprets free-text responses
into structured decisions. The interpretation is shown back to the human for
confirmation before the pipeline resumes.

Three HITL checkpoints, three interaction patterns:
- **Phase 1 (fit assessment):** conversational + free text; Haiku interprets
- **Phase 4 (section review):** lettered options + free text [e]; Haiku interprets;
  Sonnet executes revision if needed
- **Phase 5 (formatting):** binary only — Approve / Reject; no Haiku needed

**The escape hatch is load-bearing:**
Offering only lettered options makes the HITL a fancy button set. Option [e]
makes it genuinely conversational — "make the Barclays section more concise
and drop the team size mention entirely" — and the system handles it.

**The Haiku interpretation pattern:**
Haiku receives checkpoint context + human free-text, returns a structured
decision object. Small, bounded, cheap. The output is confirmed back to the
human before the pipeline resumes — the human always knows what the system
understood.

**What this teaches:**
Natural language is a better HITL interface than constrained options alone,
but only if free-text is interpreted into structured decisions before touching
the pipeline. The interpretation layer (Haiku) is what makes free-text safe
to act on. Generalises to any system where human input needs to be expressive
but downstream processing needs structured input.

**Interview framing:**
"The HITL checkpoints are conversational. A small Haiku call interprets free
text into a structured instruction before it touches the pipeline, and shows
the interpretation back to the human for confirmation. The pipeline gets
structured input; the human gets an expressive interface."
