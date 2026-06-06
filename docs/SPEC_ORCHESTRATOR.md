# SPEC_ORCHESTRATOR.md — cv-tailor
## Architecture Specification

**Project:** Week 3 Portfolio — Multi-Model Orchestration  
**Repository:** cv-tailor
**Status:** Pre-implementation (architecture agreed, no code written)  
**Last updated:** pre-build  
**Deployment target:** CLI tool, M720q home server + local use

---

## 1. Project Goals

**Learning goal:** Demonstrate orchestration patterns that sit above the single-agent loop built in Week 2. Specifically: treating other LLMs as callable tools, managing cross-provider state and cost, implementing a refinement loop with dual convergence signals, and building a dynamic evaluation rubric that evolves during execution. These are patterns that appear in every serious production agentic system.

**Real-use goal:** Automate the multi-model CV tailoring workflow currently done manually — fit assessment, keyword gap analysis, iterative critique and revision, and formatting validation — so the output is a tailored CV with a full audit trail, not just a conversation history to reconstruct by hand.

**Portfolio goal:** Demonstrate that the RAG, tool-use, and state management patterns from Weeks 1 and 2 compose into something larger. The architecture should be explainable at three levels: what it does (CV tailoring), how it works (multi-model orchestration loop), and why the design choices were made (model selection grounded in real observed behaviour, not just capability marketing).

---

## 2. What This System Does

A CLI orchestrator that takes a job description and a corpus of CV versions and produces a tailored CV with structured reasoning. The user runs one command, reviews a few HITL checkpoints, and gets an HTML output with the final CV, change diff, iteration scores, and collapsible reasoning trace.

The system has three distinct modes of operation under one CLI:

**Corpus ingestion** (one-time setup, run when CV files change):
```bash
python -m corpus.ingest --cv-dir data/cvs/
```

**CV tailoring** (the main workflow):
```bash
python -m tailor run --jd path/to/jd.txt           # full mode (key-gated)
python -m tailor run --jd path/to/jd.txt --demo    # demo mode (1 iteration, cheaper)
```

**Output review:**
```
outputs/<run_id>/cv_final.html    # full interactive view
outputs/<run_id>/cv_final.md      # clean CV (for sending)
outputs/<run_id>/run_log.jsonl    # machine-readable audit trail
```

---

## 3. Architecture Decisions

### 3.1 — Orchestration pattern: hybrid pipeline + agentic loop

**Decision:** Deterministic phases frame an agentic refinement loop. Phases 0–2 and 4–5 always run in the same order. Phase 3 (refinement) is agentic: the orchestrator decides what critique to accept, whether to update the rubric, and when to stop iterating.

**Alternatives rejected:**
*Pure pipeline* — would fix the revision sequence, losing the ability to adapt based on what critique reveals. *Pure agent loop* — would remove the predictability needed for cost management and HITL placement.

**The load-bearing reason:** Determinism where the task is known; agency where judgment is required. The refinement loop is genuinely uncertain: we don't know in advance how many iterations will be needed, which critique items are worth accepting, or whether the rubric needs updating. The framing phases are not uncertain.

**What this teaches:** Not all orchestration should be agentic. Wrapping a controlled agentic loop in deterministic scaffolding is a pattern that appears in production systems because it makes cost, latency, and HITL placement predictable.

**Interview framing:** "I deliberately split the system into deterministic phases and an agentic loop rather than making everything agentic. The refinement stage genuinely needs model judgment — when to stop, which suggestions to accept. The fit assessment and validation stages don't. Mixing the two without distinguishing them is a common design mistake."

---

### 3.2 — LLMs as tools: the key architectural extension from Week 2

**Decision:** The orchestrator (Claude Sonnet) calls other LLMs as tools, exactly as it calls database queries in Week 2. `critique_cv(draft, jd)` internally calls GPT-4o-mini. `extract_keywords(jd)` internally calls Mistral. The orchestrator never knows which model ran inside a tool — it receives a structured result.

**The abstraction:**
```python
# The orchestrator calls this like any other tool:
critique_result = critique_cv(draft=current_draft, jd=jd_analysis)

# Internally, critique_cv calls GPT-4o-mini and returns a typed object.
# The orchestrator has no visibility into the provider.
```

**Why this matters:** This makes providers swappable. If GPT-4o-mini is replaced by a future model, only the tool implementation changes — the orchestrator loop is unchanged. It also means the Week 2 tool-use pattern generalises to cross-provider orchestration without any architectural change.

**What this teaches:** The tool abstraction is not specific to function calls. Any system that returns structured output can be a tool — including other AI models. This is the fundamental insight that makes multi-model orchestration composable.

**Interview framing:** "The architecture treats LLMs as tools — the orchestrator calls `critique_cv()` the same way it calls `get_spending_summary()` in my finance agent. The provider is an implementation detail of the tool, not a concern of the orchestrator. That abstraction is what makes the system composable."

---

### 3.3 — Model routing: three providers, three distinct roles

**Decision:** Each provider handles a structurally different task, with assignment grounded in observed behaviour rather than marketing claims.

| Provider | Model | Role | Justification |
|----------|-------|------|---------------|
| Mistral | mistral-small | Keyword extraction + embeddings + scoring | Already integrated (Week 1). Structured extraction tasks. Cheaper per call than Sonnet. Consistent with "Mistral = analysis/retrieval layer." |
| Anthropic | claude-sonnet-4-6 | Orchestrator + drafter + revision decisions | Complex multi-step reasoning. Cross-turn consistency. Established from Week 2. |
| OpenAI | gpt-4o-mini | Section-by-section critique | Empirically observed: gives harsher, more direct feedback than Claude for CV review. Less likely to flatter. Different training distribution from orchestrator = genuinely independent second opinion. |
| Anthropic | claude-haiku-4-5 | Formatting validation gate | Fast, cheap, sufficient for deterministic quality checks. Model routing principle from Week 2. |

**Demo mode substitution:** Claude Haiku replaces Claude Sonnet as orchestrator in demo mode. Same interface, ~20× cheaper, adequate for a single-iteration pass.

**The load-bearing reason:** GPT's critique role is the only genuinely novel choice. The justification is empirical: direct personal observation that ChatGPT gives harsher, more actionable CV feedback than Claude. Building that observation into the architecture makes the design defensible; using GPT arbitrarily to "demonstrate multi-provider" does not.

---

### 3.4 — Dynamic scoring rubric

**Decision:** The scoring rubric is a first-class versioned object, not a static list. It is created by Mistral at the start, and can be extended by the orchestrator during the refinement loop when critique surfaces requirements not in the original JD.

**Schema:**
```python
@dataclass
class RubricAddition:
    keyword: str
    added_in_iteration: int
    triggered_by: str             # description of the critique item that surfaced this

@dataclass
class ScoringRubric:
    version: int                          # increments on each update
    required_keywords: list[str]          # from JD extraction
    nice_to_have_keywords: list[str]
    structural_requirements: list[str]    # e.g. "quantify achievements"
    added_from_critique: list[RubricAddition]  # additions during loop, with provenance
    created_at: str
    updated_at: str
```

**Why this matters:** A fixed rubric measures the CV against the JD as originally written. A dynamic rubric measures the CV against what the JD *means* — requirements that aren't explicit but are inferred by the critique model. The score is more meaningful because the evaluation criteria improve alongside the CV.

**Safeguards:**
- Maximum 2 rubric additions per iteration. Without this cap, a verbose critique model
  could inflate the rubric until keyword coverage stalls, misrepresenting CV quality.
- Each proposed addition must be validated by the orchestrator against the JD before
  acceptance: "is this actually implied by the JD, or is the critique model hallucinating
  a requirement?" Validation decision logged in audit trail.
- Additions tracked as `list[RubricAddition]` with provenance (iteration, triggering
  critique item) — not a flat list of strings.

**What this teaches:** Evaluation criteria are a design decision, not a given. In any iterative refinement system, the question "are we measuring the right things?" should be as live as "are we improving the thing we're measuring?"

---

### 3.5 — Dual-signal convergence

**Decision:** The refinement loop uses two orthogonal signals to decide when to stop:
1. **Keyword coverage score** (0–1): proportion of rubric items present in the current draft
2. **Critique quality score** (0–10): GPT's overall assessment of the draft

Convergence is declared when both deltas fall below threshold, or max_iterations is reached. The orchestrator can also declare convergence if it judges further iteration unlikely to improve either signal.

**Why two signals:** A single signal can be gamed or fail silently. A CV can score well on keywords while remaining weak on structure. A CV can receive a positive critique while missing several required terms. Two orthogonal signals provide a more robust termination condition.

**The loop termination table:**
```
If keyword_delta < 0.05 AND quality_delta < 0.5:   convergence
If iteration == max_iterations:                     hard stop
If last critique returned 0 major items:            soft-stop permitted
```

**Soft-stop constraint:** The orchestrator may declare soft-stop *only* when the
last critique returned zero major-severity items. This gives the soft-stop a
concrete, testable, and auditable trigger — not unconstrained model judgment.

---

### 3.6 — Audit trail separate from context

**Decision:** All orchestrator reasoning is logged to `run_log.jsonl` but never injected back into the messages array. Context stays clean; the audit trail is complete and inspectable after the run.

**Log entry shape:**
```json
{
  "ts": "2026-06-03T14:23:01Z",
  "phase": "refinement_loop",
  "iteration": 2,
  "event": "critique_item_rejected",
  "reasoning": "Suggested removing quantified achievement. Orchestrator rejected: JD explicitly requires metrics.",
  "keyword_score_before": 0.71,
  "keyword_score_after": 0.71,
  "critique_score_before": 7.4,
  "critique_score_after": 7.4
}
```

**What this teaches:** The same lesson from Week 2's transcript logging: observability is a first-class concern. In production systems, the ability to audit why an AI made a decision is often as important as the decision itself. Separating the audit trail from context also prevents reasoning verbosity from inflating token costs.

**Portfolio note:** The HTML output renders the audit trail as a collapsible reasoning trace. A recruiter sees a clean CV. The hiring manager asking "how does this work?" can expand every decision.

---

### 3.7 — Mode configuration (demo vs full, key-gated)

**Decision:** Runtime behaviour is governed by a `RunConfig` object loaded from `config.yaml` and optionally overridden by a key. No auth — a strong passphrase unlocks the full configuration.

```yaml
# config.yaml
modes:
  demo:
    max_iterations: 1
    orchestrator_model: "claude-haiku-4-5"
    cost_cap_usd: 0.75
    
  full:
    max_iterations: 3
    orchestrator_model: "claude-sonnet-4-6"
    cost_cap_usd: 5.00
    full_mode_key: "${FULL_MODE_KEY}"   # env var, gitignored
```

**Why config-driven rather than code-branching:** Any mode difference expressed as a config value is immediately readable, testable, and adjustable without touching code. The Week 2 pattern of `AGENT_MODEL` / `CLASSIFIER_MODEL` constants generalises here to a full `RunConfig` object.

---

### 3.8 — RAG with metadata over context window

**Decision:** The 6 CV versions are embedded and stored in ChromaDB with structured metadata. Phase 1 retrieves the best-matching CV via metadata-filtered semantic search, not by passing all CVs in a single context window.

**Why RAG over context for 6 short documents:** The immediate answer is it's a fair question — 6 CVs would fit in a single context window today. The real answer is twofold: (1) the collection will grow; new versions should be searchable without re-prompting; (2) reusing the Week 1 infrastructure demonstrates composition of existing components, which is the portfolio point. Passing everything in context is not architecturally interesting.

**ChromaDB ingestion unit: section, not CV.**
Each section of each CV is a separate ChromaDB document. CV-level metadata is
replicated on every section document so filtered queries work at either level.

**Metadata schemas:**
```python
@dataclass
class CVSection:
    section_id: str       # stable unique id, e.g. "experience_acme_corp_principal_2022"
    section_type: str     # canonical type from config.yaml cv_sections list
    position: int         # order within this CV (0-indexed); governs final assembly order
    static: bool = False  # if True: copied as-is, excluded from critique and scoring loop
                          # interests: always True; education/certifications: usually True
    word_count: int = 0   # measured from source docx at ingestion
    line_count: int = 0   # approximate rendered lines (word_count / avg_words_per_line)

@dataclass
class CVMetadata:
    filename: str
    cv_type: str              # "generic" | "job_specific"
    target_role: str          # e.g. "Solution Architect"
    target_company: str | None
    skills_emphasis: list[str]
    seniority: str            # "senior" | "principal" | "director"
    version_date: str
    sections: list[CVSection] # ordered, present sections only
                              # absence from list = section not in this CV version
                              # (e.g. ai_projects absent from management-focused CVs)
```

**Canonical section list (config.yaml):**
> Updated during Step 1 build from the observed corpus (LEARNING_NOTES F-05,
> D-19/D-20/D-21): `header` and `languages` added; experience sub-sections are
> per *company* (`experience_<company_slug>`), not per job; section detection is
> by canonical-name vocabulary (`section_aliases`) + a size split inside the
> experience block, not by heading style.
```yaml
cv_sections:
  - header            # static, position 0 — name + contact block (above Profile)
  - profile
  - skills
  - experience        # one CVSection per COMPANY; section_id = experience_<company_slug>
  - ai_projects       # absent from some CVs; prominent in AI-role CVs
  - education         # static: true (typically)
  - languages         # static, present in this corpus
  - certifications    # static: true (typically); absent from this corpus
  - interests         # static: true, always
```

**Length budget derived at ingestion:**
After ingesting all CVs, the ingestion script derives a SectionBudget per
section_type from observed word counts across the corpus:

```python
@dataclass
class SectionBudget:
    section_type: str
    min_words: int      # smallest this section appears across corpus
    max_words: int      # largest
    target_words: int   # median — working budget for drafting and critique
```

Budgets written to budgets.yaml after ingestion. Total target word count across
all sections is the two-page envelope, derived from actual CVs rather than
guessed. The orchestrator uses target_words as the drafting target. The critique
prompt flags length violations: major if a section exceeds max_words (breaks the
two-page constraint), minor if materially below min_words (undertells the role).
Phase 5 (Haiku) does a final assembled-CV length check against total budget.

**Retrieval strategy:** Metadata pre-filter (role/seniority match from JD analysis)
→ semantic scoring within filtered subset → return top-2 CV matches with scores for
orchestrator to decide between. Section-level retrieval also available for Phase 1
fit reasoning (e.g. "best experience section for a solution architect role").

---

### 3.9 — CLI-first, HTML output IS the review interface

**Decision:** No web UI. The HTML output file serves as the review interface. One-time ingestion runs as a CLI command. The tailoring run is a CLI command with HITL prompts in the terminal.

**Why no web UI:** The output IS a document, not a conversation. The HITL is two or three decisions per run, not an ongoing session. Adding a web UI would add FastAPI + React scope without adding learning value not already covered in Week 2 (C4). The HTML output provides the visual richness that the web UI would otherwise provide.

---

### 3.9 — Candidate Value Creation Model (CVCM)

**What it is:** A durable markdown file (`candidate/value_creation_model.md`)
authored and maintained by the candidate. It captures how the candidate
consistently creates value across roles, independent of job titles, industries,
or specific achievements — the recurring patterns that explain why organisations
hire, trust, promote, and retain them.

**What it is not:** Generated by the system, modified by the system, or required
for the pipeline to run. It is optional candidate context that shifts tailoring
from keyword optimisation toward articulation of authentic value.

**Example attributes captured:**
- Problem-solving approach and decision-making patterns
- Leadership philosophy and stakeholder engagement style
- Recurring career themes and value creation mechanisms
- Technical vs commercial balance
- Types of business problems repeatedly solved
- Preferred operating environments

**Design principle:** The orchestrator consumes the CVCM; it never writes to it.
The candidate authors it once, reviews and refines it over time, and it persists
across all applications as a durable artifact.

**Phase 1 integration:**
Loaded alongside `JDAnalysis` and the CV sections. Fit assessment gains a fourth
evaluation dimension — value creation alignment:
1. Role alignment
2. Skill alignment
3. Experience alignment
4. Value creation alignment — *which aspects of the candidate's value creation
   model are most relevant to the business problem this employer is trying to solve?*

`FitAssessment.value_alignment_notes: str | None` populated when CVCM present.
Surfaced in the HITL display.

**Phase 2 integration:**
Passed to the Claude Sonnet drafting prompt as candidate context. Writers
instructed to: preserve candidate differentiation; frame experience through
recurring value creation patterns; avoid reducing the candidate to keyword
optimisation alone.

**Phase 3 integration:**
Passed to both writers and the orchestrator. When two drafts achieve similar
quality scores (`|claude_quality - gpt_quality| < 1.0`, the existing tiebreak
band from D-28), the orchestrator uses the CVCM as a secondary selection factor:
preference given to the draft that better articulates authentic value.

**Gitignore:** `candidate/` is gitignored. Contains personal career data.

---

## 4. Schemas

All inter-stage communication uses typed dataclasses. Each is JSON-serialisable and saved to `outputs/<run_id>/` at the end of its producing stage (checkpoint pattern from Week 2).

```python
@dataclass
class JDAnalysis:
    raw_text: str
    role_title: str
    seniority_level: str          # inferred
    key_requirements: list[str]
    nice_to_haves: list[str]
    company_context: str
    tone_signals: list[str]       # e.g. "technical", "startup", "formal"

@dataclass
class RubricAddition:
    keyword: str
    added_in_iteration: int
    triggered_by: str             # description of the critique item that surfaced this

@dataclass
class ScoringRubric:
    version: int
    required_keywords: list[str]
    nice_to_have_keywords: list[str]
    structural_requirements: list[str]
    added_from_critique: list[RubricAddition]  # additions during loop, with provenance
    created_at: str
    updated_at: str

@dataclass
class CVSection:
    section_id: str
    section_type: str
    position: int
    static: bool = False
    word_count: int = 0
    line_count: int = 0

@dataclass
class CVMetadata:
    filename: str
    cv_type: str
    target_role: str
    target_company: str | None
    skills_emphasis: list[str]
    seniority: str
    version_date: str
    sections: list[CVSection]

@dataclass
class SectionBudget:
    section_type: str
    min_words: int
    max_words: int
    target_words: int             # median across corpus — working budget for drafting

@dataclass
class CVMatch:
    filename: str
    metadata: CVMetadata
    semantic_score: float
    keyword_coverage: float       # against initial rubric
    # Internal retrieval utility — not the output of Phase 1

@dataclass
class SectionRecommendation:
    section_id: str
    source_cv: str          # filename of the CV this section is drawn from
    section_version: str    # which version file within that CV
    keyword_coverage: float # this section's coverage against initial rubric
    reason: str             # one-line: "best Skills coverage for ML role"

@dataclass
class FitGap:
    requirement: str
    gap_type: str           # "keyword" | "experience" | "hard_requirement" | "seniority"
    addressable: bool       # True = CV tailoring can close this; False = cannot
    severity: str           # "minor" | "major" | "blocking"
    reason: str             # one-line: "JD requires SC clearance; not in any CV version"

@dataclass
class FitAssessment:
    outcome: str            # "strong" | "partial" | "no_fit"
                            # no_fit = blocking gap found; stop pipeline here
    recommended_sections: dict[str, SectionRecommendation] | None
                            # section_id → best source section across all CVs
                            # None when outcome == "no_fit"
    skills_transferable: list[str]
    gaps: list[FitGap]      # typed gaps replacing flat skills_gaps list
    overall_fit_score: float         # 0–1; weighted mean across section coverages
    no_fit_reason: str | None        # plain-English explanation when outcome == "no_fit"
                                     # e.g. "JD requires active SC clearance (non-negotiable);
                                     #       none of your CV versions mention this.
                                     #       CV tailoring cannot resolve a missing credential."
    value_alignment_notes: str | None  # CVCM integration: which aspects of the candidate's
                                       # value creation model are most relevant to this role.
                                       # None when no CVCM file present (pipeline runs normally).

@dataclass
class SectionScore:
    section_id: str
    section_type: str
    keyword_coverage: float      # 0–1; union coverage across section text (F-15)
    claude_quality: float | None # orchestrator's score of Claude's draft (0–10); None if frozen
    gpt_quality: float | None    # orchestrator's score of GPT's draft (0–10); None if frozen
    selected_writer: str | None  # "claude"|"gpt"|"synthesis"; None if static or frozen
    converged: bool              # True = frozen for remaining iterations
    current_version: int         # version number of the selected draft written to disk

@dataclass
class IterationScore:
    iteration: int
    section_scores: dict[str, SectionScore]   # section_id → SectionScore
    keyword_coverage: float       # UNION coverage across non-static sections (F-15):
                                  #   the CV-level "fraction of the rubric covered
                                  #   anywhere". (Earlier draft said "weighted mean";
                                  #   union matches the 61→74→83% example below and F-11.)
    critique_score: float | None  # mean of selected-draft quality across active sections;
                                  #   None when every non-static section is frozen.
                                  #   "selected draft" = whichever writer the orchestrator
                                  #   chose (claude_quality or gpt_quality from SectionScore).
                                  #   NB: as easy sections freeze, mean is over harder
                                  #   survivors — can dip even as CV improves (F-16).
                                  #   The 0.5 delta threshold absorbs this.
    keyword_delta: float          # vs previous iteration aggregate
    quality_delta: float          # delta in critique_score (renamed from critique_delta
                                  #   for clarity — same convergence threshold: < 0.5)
    sections_converged: int       # count of newly frozen sections this iteration
    sections_active: int          # count still being critiqued

@dataclass
class CritiqueItem:
    section: str
    severity: str                # "major" | "minor"
                                 # major = materially weakens the application or contradicts JD
                                 # minor = improvement opportunity; CV is acceptable without it
                                 # Defined in both writer system prompts — consistent labels
                                 # required because soft-stop depends on zero major items
    issue: str
    suggestion: str
    source_writer: str           # "claude" | "gpt" — which writer raised this item

@dataclass
class WriterDraft:
    writer: str                  # "claude" | "gpt"
    section_id: str
    text: str
    version: int                 # mirrors iteration number; v0 = Phase 2 initial draft
    pushback: str | None         # writer's reasoning when disagreeing with orchestrator direction
                                 # None on first iteration (no prior direction to push back on)
    items: list[CritiqueItem]    # issues the writer flags in its own draft
                                 # soft-stop and freeze depend on zero major items across
                                 # both writers — these are the canonical source for that check

@dataclass
class OrchestratorDecision:
    section_id: str
    selected_base: str           # "claude" | "gpt" | "synthesis"
    direction: str               # what both writers should focus on next iteration
    synthesis_notes: str | None  # if selected_base == "synthesis": what to take from each
    keyword_coverage: float      # scored against rubric on selected/synthesised text
    claude_quality: float        # orchestrator's score of Claude's draft (0–10)
    gpt_quality: float           # orchestrator's score of GPT's draft (0–10)
    converged: bool              # orchestrator judges this section done (both drafts strong,
                                 # zero major items) — consistent with loop soft-stop condition
    rubric_additions: list[str]  # new requirements surfaced this decision (max 2, JD-validated)

# Note: Critique class removed. Writers self-assess and return CritiqueItems
# inside WriterDraft.items. There is no separate GPT critique call.

@dataclass
class LoopMemory:
    rejected_suggestions: list[str]     # accumulates across iterations — prevents re-litigation
    orchestrator_directions: list[str]  # one per completed iteration (writers see trajectory)
    frozen_sections: list[str]          # section_ids excluded from further dual-write calls
    iteration_scores: list[IterationScore]  # score history

@dataclass
class ReasoningEntry:
    ts: str
    phase: str
    iteration: int | None
    event: str
    reasoning: str
    keyword_score: float | None
    critique_score: float | None
    rubric_version: int | None    # rubric version active when scores were computed

@dataclass
class PipelineOutput:
    run_id: str
    mode: str
    base_cv_filename: str
    jd_analysis: JDAnalysis
    fit_assessment: FitAssessment
    final_rubric: ScoringRubric
    iterations: list[IterationScore]
    final_cv_md: str
    # Note: intermediate drafts (draft_v0.md … draft_vN.md) are NOT stored here.
    # Each phase writes its draft to outputs/<run_id>/ as a checkpoint.
    # Phase 6 reads draft_v*.md from disk to build the Changes tab diffs.
    # PipelineOutput is a summary object, not a data warehouse.
    cost_breakdown: dict[str, float]   # per model: "anthropic_sonnet", "anthropic_haiku",
                                        # "openai_gpt4o_mini", "mistral_small"
    converged: bool
    convergence_reason: str
```

---

## 5. Orchestration Phases

### Phase 0 — JD Analysis (Mistral)

**Input:** Raw JD text  
**Output:** `JDAnalysis` + initial `ScoringRubric` (v1)  
**Model:** Mistral Small (forced structured output via tool_choice)

Mistral extracts requirements, seniority signals, tone, and produces the initial keyword list. The same Mistral integration from Week 1 — different task (extraction vs retrieval) but same client.

---

### Phase 1 — Fit Assessment (Mistral RAG + Claude Sonnet)

**Input:** `JDAnalysis`, `ScoringRubric`, `candidate/value_creation_model.md` (optional)
**Output:** `FitAssessment`
**Models:** Mistral embeddings (retrieval) + Claude Sonnet (fit reasoning)

RAG query: embed the JD requirements, retrieve top-k sections per section_type
from ChromaDB using metadata pre-filter (seniority and role match). Claude Sonnet
evaluates each retrieved section against the rubric and builds a section-level
composition recommendation — the best source section for each section_type across
the full corpus, not a single best CV.

If `candidate/value_creation_model.md` is present, it is loaded and passed to the
fit reasoning prompt. The assessment gains a fourth dimension — value creation
alignment — and `FitAssessment.value_alignment_notes` is populated. If absent,
the pipeline runs normally with no degradation to structural fit scoring.

**Three outcomes:**

`no_fit` — one or more blocking gaps found that CV tailoring cannot resolve
(missing credential, hard seniority mismatch, hard requirement absent from all
CVs). Pipeline stops here. No drafting, no API spend beyond Phase 1. The
`no_fit_reason` field provides a plain-English explanation.

`partial` — addressable gaps exist. Orchestrator proceeds but flags the gaps
prominently in the HITL display. Human decides whether to continue.

`strong` — no blocking gaps. Proceed with confidence.

**HITL checkpoint (Phases 1 all three outcomes):**

```
─── Fit Assessment ──────────────────────────────────────────
  Outcome: PARTIAL FIT  (overall: 74%)

  Recommended section mix:
  ✓ profile       → solution_architect_generic_v3   (81% coverage)
  ✓ skills        → solution_architect_ai_v2        (88% coverage, best ML tooling)
  ✓ exp_acme      → solution_architect_generic_v3   (76% coverage)
  ✓ exp_barclays  → solution_architect_generic_v3   (71% coverage)
  ✓ ai_projects   → solution_architect_ai_v2        (only CV with this section)
  — education     → static
  — interests     → static

  Transferable: cloud architecture, stakeholder management, delivery at scale
  Gaps:
  ⚠  Kubernetes experience  [experience / major / addressable]
  ⚠  P&L ownership          [experience / major / addressable]

  Options: [p]roceed  [a]adjust section mix  [s]top
```

For `no_fit`, the display replaces the section mix with the blocking gap
explanation and only offers `[s]top` or `[o]verride and proceed anyway`.

In the web UI, this checkpoint is a conversational HITL (see §UI).

---

### Phase 2 — Initial Draft (Claude Sonnet)

**Input:** `FitAssessment` (specifically `recommended_sections`), `JDAnalysis`, `ScoringRubric`, `budgets.yaml`, `candidate/value_creation_model.md` (optional)
**Output:** `draft_v0/` directory — one `<section_id>_v0.md` file per non-static section; static sections copied as `<section_id>_static.md`
**Model:** Claude Sonnet

Orchestrator drafts each non-static section independently from its recommended
source section (per `FitAssessment.recommended_sections`), respecting the
`target_words` budget. If the human adjusted the section mix at the Phase 1
HITL, those overrides are reflected here. Static sections copied verbatim.

If CVCM is present, it is passed to the drafting prompt as candidate context.
Writers are instructed to: preserve candidate differentiation; frame experience
through recurring value creation patterns; avoid reducing the candidate to
keyword optimisation alone.

Section files written to `outputs/<run_id>/sections/` as checkpoints.

---

### Phase 3 — Refinement Loop (dual-writer; Claude Sonnet orchestrates)

**Input:** Current section files, `JDAnalysis`, `ScoringRubric`, `budgets.yaml`
**Output:** Updated section files, updated `ScoringRubric`, `IterationScore`, `ReasoningEntry` list
**Models:** Claude Sonnet (writer + orchestrator) + GPT-4o-mini (writer)

Mirrors the manual workflow that produced the best real-world CV results: two
independent writers with different priors, adjudicated by an orchestrator that
can select, synthesise, or push both writers back for another pass.

**Each iteration, per active (non-frozen) section:**

```
Step 1 — Dual write
  Claude Writer      → write_section(...) → WriterDraft (text + items + pushback)
  GPT-4o-mini Writer → write_section(...) → WriterDraft (text + items + pushback)
  Both writers receive:
    - current section text (source v0 or prior iteration's selected version)
    - JD requirements + rubric
    - word budget: clamp(source_word_count, min, max) per D-27/F-13
    - orchestrator direction from prior iteration (None on iter 1)
    - rejected_suggestions list (accumulates; prevents re-litigation)
    - is_final_iteration: bool ("definitive version" prompt on last pass)
    - CVCM content (optional; passed when candidate/value_creation_model.md present)
  Each writer self-assesses and returns CritiqueItems in WriterDraft.items

Step 2 — Orchestrator adjudication (Claude Sonnet, orchestrator role)
  Sees: both WriterDrafts (text + items), rubric, keyword scores
  Produces OrchestratorDecision per section:
    selected_base ("claude"|"gpt"|"synthesis"),
    synthesis_notes (what to take from each, if synthesis),
    direction for next iteration,
    claude_quality + gpt_quality (0–10, same anchors as writer prompts),
    keyword_coverage of selected text,
    converged (True when both drafts strong + zero major items),
    rubric_additions (max 2 total per iteration, JD-validated)
  When |claude_quality - gpt_quality| < 1.0 AND CVCM present:
    CVCM used as secondary tiebreaker — preference to draft that better
    articulates candidate's authentic value creation model (D-33).
  Selected/synthesised text written to disk as: <section_id>_v<n>.md
  Per-writer drafts also written for inspection: <section_id>_<writer>_v<n>.md

Step 3 — Writer pushback (if not final iteration)
  Both writers read OrchestratorDecision + direction
  May push back with explicit reasoning (WriterDraft.pushback: str | None)
  Orchestrator reads both pushbacks; may revise direction or hold
  One exchange only — pushback is not subject to further pushback
  Revised direction feeds Step 1 of next iteration

Step 4 — Convergence check
  Per-section freeze: OrchestratorDecision.converged == True
  Loop soft-stop: zero major items across both writers' WriterDraft.items
  Both conditions are kept consistent — orchestrator is instructed to set
  converged=True only when it also sees zero major items from both writers
  Frozen sections excluded from all subsequent Steps 1–3
```

**Loop-level memory (LoopMemory — D-06 compatible structured state):**
Forwarded into every writer and orchestrator call each iteration:
```python
@dataclass
class LoopMemory:
    rejected_suggestions: list[str]     # accumulates — prevents re-litigation
    orchestrator_directions: list[str]  # one per completed iteration
    frozen_sections: list[str]
    iteration_scores: list[IterationScore]
```

**Prompt caching (D-31):**
Anthropic `cache_control` breakpoints on the stable prefix (system prompt, then
role + JD requirements + rubric); variable content (drafts, direction, LoopMemory)
in the user message after the prefix. OpenAI caches qualifying prefixes
automatically (stable content placed first). **Measured, not assumed (F-22):** the
real stable prefix is only ~534 tokens — under *both* provider minimums (Sonnet
1024, Haiku 2048) — so caching is currently a **no-op** at this prompt scale. The
wiring is proven correct (a 4202-token control gives a 100% cache read) and engages
automatically if prompts grow; it is kept because it is costless below the minimum
and scales without change. Don't pad prompts to force it: the cacheable bulk is
small, so even when active the saving is sub-cent per iteration.

**Termination table (thresholds confirmed on real data — F-16):**
```
keyword_delta < 0.05 AND quality_delta < 0.5  →  convergence
iteration == max_iterations                    →  hard stop
zero major items (both writers, last iter)     →  soft-stop permitted
```

**Cost model:**
Per active section per iteration: 2 writer calls + 1 orchestrator call + 2 pushback calls.
Sections freeze progressively (F-16: 5–6 of 8–10 freeze after iter 1 on real data).
Estimated full-mode run: ~$2–4. Accepted trade-off for dual-writer quality uplift.

---

### Phase 4 — Human Review (HITL)

**Input:** Final section files, `IterationScore` list, unresolved critique items
**Output:** Human-confirmed or adjusted sections

Terminal display (CLI mode). Under the dual-writer loop, the quality line is the
**selected** draft's quality, and "unresolved items" are the writers'
self-assessed items on sections that never converged (surfaced via
`RefinementResult.unresolved`):
```
─── Refinement complete ───────────────────────────────────
  Iterations:       3 / 3  (max_iterations)
  Keyword coverage: 61% → 74% → 83%
  Quality (sel.):   6.2 → 7.8 → 8.4

  Section status:
  ✓ Profile              converged iter 1  (v1)
  ~ Barclays             active            (v3)  ← did not converge
  — Interests            static

  Unresolved items (1):
  [1] Barclays: "quantify team size" (minor)

  Options:
  [a] Accept all and proceed
  [b..] Apply unresolved item by number (e.g. b1)
  [d] Leave all unresolved and proceed
  [e] Something else — describe what you want
```

Option [e] accepts free-text: Claude Haiku interprets it into a structured
`{section_id, instruction}` (shown back to the human first — preview-before-apply),
then a single Claude **writer** pass executes the revision (reusing
`tools/claude_writer.py`, not a new tool). This is the conversational escape
hatch (F-24).

In the web UI, this entire checkpoint is a conversational HITL (see §UI).

---

### Phase 5 — Formatting Validation (Claude Haiku)

**Input:** Human-confirmed section files, `budgets.yaml`
**Output:** Validated section files + list of formatting corrections

Checks per section: punctuation consistency, em-dash / en-dash / hyphen usage,
bullet point parallelism, tense consistency, date format consistency.

Final assembled-CV length check: sum of section word counts vs total budget
derived from `budgets.yaml`. If assembled CV materially exceeds total budget,
surfaces the longest sections for human review before output.

Returns corrected versions + diff per section. HITL is **yes/no only**:
corrections are shown as a diff, human confirms or rejects in one response.
No free-text at this stage — formatting corrections are applied as-is or
not at all. In the web UI: two buttons, Approve / Reject.

---

### Phase 6 — Output Generation

**Input:** All phase outputs, section files, `run_log.jsonl`
**Output:** `cv_final.html`, `cv_final.md`

**Assembly:** Reads section files from `outputs/<run_id>/sections/`. For each
section_id, uses the highest accepted version (or static copy). Orders sections
by **(config `cv_sections` type order, then source `position`)** — both carried in
the Phase 2 manifest, so assembly is checkpoint-driven and never re-queries the
corpus. (The original "position from the base CV" doesn't apply under
section-mixing — sources differ per section; section_type is the primary key and
`position` only a within-type tiebreak, mainly the experience block. F-23.)
Assembles into a single document before rendering.

Experience **role/date lines** are re-attached here, not drafted (F-29): the
company heading is the section `title`, and the role line(s) live in the manifest's
`role_line` (split off at Phase 2 so the drafter can't drop them). They render bold
between the heading and the bulleted body — this is what keeps two role-groups at
one employer (D-21) distinct and preserves job titles + dates verbatim.

HTML structure:
```
[CV tab]        Clean assembled CV — printable view
[Changes tab]   Per-section diffs: each section shows version progression
                (profile: v0→v1→v2, experience_acme: v0→v1→v2→v3, etc.)
[Scores tab]    Per-section keyword coverage + critique score per iteration;
                section freeze events annotated; aggregate scores as summary row
[Reasoning tab] Collapsible audit trail by phase and section
```

---

## 6. CLI Design

All CLI commands run inside Docker. The `cli` service is short-lived and
interactive; the project directory is bind-mounted so outputs persist on the host.

```bash
# One-time setup
cp .env.example .env    # add ANTHROPIC_API_KEY, OPENAI_API_KEY, MISTRAL_API_KEY, FULL_MODE_KEY
docker compose build

# One-time corpus ingestion (run when CVs change)
docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
  Flags:
    --cv-dir PATH          Directory of CV .docx files (required)
    --collection NAME      ChromaDB collection name (default: "cv_corpus")
    --replace              Re-ingest files that already exist

# Main tailoring run
docker compose run --rm cli python -m tailor run --jd PATH [--demo] [--key KEY]
  Flags:
    --jd PATH              Path to JD text file (required)
    --demo                 Demo mode: 1 iteration, Haiku orchestrator, $0.75 cap
    --key KEY              Full mode passphrase (or set FULL_MODE_KEY env var)
    --output-dir PATH      Where to write outputs (default: outputs/<run_id>/)
    --max-iterations N     Override config value
    --dry-run              Parse JD + assess fit only; no drafting or critique
    --yes                  Non-interactive: accept every HITL checkpoint (AutoHITL)
    --docx                 Also write cv_final.docx in the source CV's formatting (F-33)

# Inspect a past run
docker compose run --rm cli python -m tailor replay <run_id>
  Shows: run summary, iteration scores, cost breakdown
  Flags:
    --reasoning            Include full reasoning trace

# Run tests
docker compose run --rm cli pytest tests/

# Web UI (dev)
docker compose up backend frontend
# → http://localhost:3000
```

---

## 7. System Components

```
cv-tailor/
├── corpus/
│   ├── ingest.py              ← CLI: embed CVs + store in ChromaDB with metadata
│   ├── retrieval.py           ← metadata-filtered semantic search
│   └── metadata.py            ← CVMetadata schema + YAML front-matter parser
├── tailor/
│   ├── __main__.py            ← CLI entry: `python -m tailor`
│   ├── run.py                 ← main orchestration loop + phase sequencing
│   ├── phases/
│   │   ├── phase0_jd_analysis.py       ← Mistral keyword extraction
│   │   ├── phase1_fit_assessment.py    ← RAG retrieval + fit scoring
│   │   ├── phase2_initial_draft.py     ← Claude Sonnet draft generation
│   │   ├── phase3_refinement.py        ← refinement loop + convergence
│   │   ├── phase4_hitl.py             ← terminal HITL display + input (CLI)
│   │   ├── phase5_validation.py        ← Haiku formatting gate
│   │   └── phase6_output.py           ← HTML + markdown generation
│   ├── tools/
│   │   ├── claude_writer.py     ← Claude Sonnet writer (draft + self-assessed items + pushback)
│   │   ├── gpt_writer.py        ← GPT-4o-mini writer (draft + self-assessed items + pushback)
│   │   ├── orchestrator_tool.py ← Claude Sonnet orchestrator role (adjudication + scoring)
│   │   ├── scorer.py            ← keyword coverage (token-subset match per D-25/F-10)
│   │   └── rubric.py            ← ScoringRubric management + JD-validation logic
│   ├── models.py              ← all dataclasses (schemas §4)
│   ├── config.py              ← RunConfig loader from config.yaml
│   ├── audit.py               ← ReasoningEntry logger → run_log.jsonl
│   └── helpers.py             ← multi-provider clients + call_with_retry()
├── api/                       ← FastAPI backend (UI phase)
│   ├── main.py
│   ├── routers/
│   │   ├── corpus.py          ← /api/corpus/* endpoints
│   │   ├── runs.py            ← /api/runs/* + SSE stream
│   │   └── hitl.py            ← /api/runs/{id}/hitl
│   ├── session.py             ← run session management + TTL cleanup
│   └── CLAUDE.md              ← backend conventions (added during UI build)
├── frontend/                  ← React + Vite + shadcn/ui (UI phase)
│   ├── src/
│   ├── Dockerfile.prod        ← multi-stage: Node build → nginx-alpine
│   └── CLAUDE.md              ← frontend conventions (added during UI build)
├── templates/
│   └── output.html            ← HTML output template (Jinja2)
├── candidate/                 ← durable candidate artifacts (gitignored)
│   └── value_creation_model.md ← CVCM: authored + maintained by candidate;
│                                  consumed by Phases 1/2/3; never generated
│                                  or modified by the system. Optional — pipeline
│                                  runs without it, with degraded authenticity.
├── data/
│   ├── cvs/                   ← source CV .docx files (gitignored)
│   └── chroma/                ← ChromaDB persistence directory (gitignored)
├── outputs/                   ← run outputs (gitignored)
│   └── <run_id>/
│       ├── sections/
│       ├── phase0_jd_analysis.json
│       ├── phase1_fit_assessment.json
│       ├── cv_final.html
│       ├── cv_final.md
│       └── run_log.jsonl
├── tmp/                       ← per-session ephemeral state (UI phase, gitignored)
├── budgets.yaml               ← derived at ingestion; SectionBudget per section_type
├── tests/
│   ├── test_schemas.py
│   ├── test_scorer.py
│   ├── test_corpus.py
│   └── test_phases.py
├── config.yaml                ← demo/full mode config
├── Dockerfile                 ← Python 3.13 slim; pipeline + api in one image
├── docker-compose.yml         ← dev: cli + backend services, bind-mounts
├── docker-compose.prod.yml    ← prod overlay: no --reload, restart policies,
│                                 nginx frontend, read-only source mounts
├── requirements.txt
├── .env                       ← ANTHROPIC_API_KEY, OPENAI_API_KEY,
│                                 MISTRAL_API_KEY, FULL_MODE_KEY (gitignored)
├── .env.example
├── .dockerignore              ← excludes data/, chroma/, outputs/, .env, tmp/
└── ADAPTING.md                ← how to use this project with your own CVs
```

---

## 7.5 — Docker Setup

**One image, three entry points.** The Python image serves the CLI (corpus
ingestion + tailoring runs), the FastAPI backend, and pytest. No separate
images for pipeline vs API — they share the same codebase and the same
`Dockerfile`.

```dockerfile
# Dockerfile — Python 3.13 slim
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# No CMD — entry point is specified per service in compose
```

**Two compose files — dev and prod overlay (same pattern as RFI project):**

```yaml
# docker-compose.yml — dev
services:
  cli:
    build: .
    volumes:
      - .:/app                    # bind-mount: edits on host visible immediately
      - /app/frontend/node_modules  # exclude host node_modules if present
    env_file: .env
    # Usage: docker compose run --rm cli python -m corpus.ingest --cv-dir data/cvs/
    #        docker compose run --rm cli python -m tailor run --jd data/jd.txt --demo
    #        docker compose run --rm cli pytest tests/

  backend:
    build: .
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
    ports:
      - "8000:8000"
    volumes:
      - .:/app
    env_file: .env

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile.dev   # Vite dev server
    ports:
      - "3000:3000"
    volumes:
      - ./frontend:/app
      - /app/node_modules
```

```yaml
# docker-compose.prod.yml — homeserver overlay
services:
  backend:
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000  # no --reload
    restart: unless-stopped

  frontend:
    image: cv-tailor-frontend-prod  # distinct name so a prod build doesn't clobber
                                     # the dev Vite image (same default name) — F-32
    build:
      context: ./frontend
      dockerfile: Dockerfile.prod   # multi-stage: Node build → nginx-alpine (~50 MB)
    restart: unless-stopped
    # nginx serves static bundle on :3000, proxies /api/* to backend:8000
    # nginx.conf: proxy_buffering off for SSE streams.
    # NB: Compose concatenates volumes across -f files, so the dev frontend mounts
    # survive into the merged prod config — inert under nginx (serves the baked
    # bundle at /usr/share/nginx/html, not /app), so the production bundle ships (F-32).
```

**Deployment to homeserver (M720q):**

```bash
# 1. Clone repo on server
git clone <repo> /srv/cv-tailor && cd /srv/cv-tailor

# 2. Set API keys
cp .env.example .env && $EDITOR .env

# 3. Seed corpus from dev machine (optional)
rsync -avz data/chroma/ server:/srv/cv-tailor/data/chroma/
rsync -avz data/cvs/    server:/srv/cv-tailor/data/cvs/
rsync -avz budgets.yaml server:/srv/cv-tailor/

# 4. Build + start
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Frontend: http://server:3000
# Backend:  http://server:8000
```

**Updates:**
```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**Bind-mounted state (back up these, nothing else):**
- `data/chroma/` — ChromaDB (re-embeddable but costs API calls)
- `data/cvs/` — source .docx files (irreplaceable)
- `budgets.yaml` — derived at ingestion
- `.env` — API keys

**`.dockerignore`** excludes everything that must not land in the image:
```
data/cvs/
data/chroma/
outputs/
tmp/
.env
frontend/node_modules/
frontend/dist/
```

**nginx note for SSE:** `proxy_buffering off` in nginx.conf is required for
SSE streams to reach the browser without buffering delay. Same finding as
the RFI project (LEARNING_NOTES_RFI entry 26).

---



Each step produces something independently testable before the next begins.

**Step 0 — Schemas and shared models**
Define all dataclasses in `models.py` including `CVSection`, `CVMetadata`,
`SectionBudget`, `SectionScore`, `IterationScore` (section-granular), and the
updated `RubricAddition` / `ScoringRubric`. Write `test_schemas.py`
(serialisation round-trips, required fields). Build `audit.py` — pure typed
logger, no API calls, needed from Phase 2 onward. No API calls in this step.

*Verification:* `pytest tests/test_schemas.py` passes with no API calls.

**Step 1 — Corpus ingestion + ChromaDB**
Build `corpus/ingest.py`, `corpus/retrieval.py`, `corpus/metadata.py`.

Key sub-tasks:
- `.docx` parser (reused from the Week 1 RAG `docx_loader`): flatten the
  single-table layout into `Paragraph`s with rendered size / bold / numPr / date.
  Section boundary detection is by **canonical-title vocabulary + size split**,
  NOT heading styles (corrected during build — see LEARNING_NOTES F-04/F-05,
  D-19/D-20/D-21). The real 7-CV corpus has no reliable heading structure; a
  style-only parser would silently produce a near-empty corpus.
- **Section extraction verification pass:** after parsing each CV, print a section
  inventory (`section_id: N words`) and block ingestion if any section count is below
  a minimum threshold (< 4 sections on a 2-page CV is almost certainly a parsing
  failure). Matched-but-empty headers are reported, not silently dropped. Human
  must confirm before ChromaDB writes proceed.
- **Collection metric verification:** after `get_or_create`, verify
  `collection.metadata["hnsw:space"] == config.metric`. If mismatch, raise with:
  "Collection exists with metric X, config requires Y. Run --replace to recreate."
- **Metadata sanitisation:** `sanitise_metadata(d: dict) -> dict` in `ingest.py`
  strips None values and empty strings before any `collection.add()` call.
  ChromaDB rejects None and empty string metadata values silently in some versions.
- Persisted discovery (R-10): section structure + CV metadata are stored as
  ChromaDB section metadata and in the per-CV sidecar `<name>.yaml`; the
  tailoring path treats this as ground truth and never re-derives it. (The spec
  originally said per-section YAML front-matter files; implemented as ChromaDB
  metadata + sidecar instead — section files are written later, in Phase 2.)
- `SectionBudget` derivation: compute min/max/median word counts per section_type,
  write `budgets.yaml`
- Checkpoint granularity: per CV — ChromaDB persists after each CV, so a failure
  loses at most one CV's embeddings (the natural failure unit for a 7-CV corpus;
  embedding is batched per CV). Refines R-06's per-section guidance for this scale.

*Verification:* a role-relevant query (e.g. "solutions consulting leadership
experience") returns the right experience sections; `budgets.yaml` exists with
plausible word counts; re-ingest without `--replace` skips existing CVs;
collection metric matches config. (All confirmed: 83 sections, 7 CVs.)

**Step 2 — JD analysis and scoring rubric**
Build `phases/phase0_jd_analysis.py` and `tools/scorer.py`.
`scorer.py` now operates at section level: `keyword_coverage(section_text, rubric)`
returns a float for a single section. Aggregate score is derived from section scores.

*Verification:* `keyword_coverage(real_section, rubric_from_real_jd)` returns
a plausible float; `ScoringRubric` from a real JD looks sensible.

**Step 3 — Fit assessment**
Build `phases/phase1_fit_assessment.py`.
Wire RAG retrieval to rubric scoring. Test: given a JD, does it recommend the
right CV? Verify HITL display is readable.

*Verification:* correct CV recommended for a known JD; fit score and gaps display cleanly.

**Step 4 — Initial draft (section-granular)**
Build `phases/phase2_initial_draft.py`.
Drafts each non-static section independently using `target_words` from `budgets.yaml`.
Static sections copied verbatim. Writes section files to `outputs/<run_id>/sections/`.

*Verification:* section files exist; static sections match source; drafted sections
respect word budget (within ~10%); aggregate keyword coverage higher than base CV.

**Step 5 — Writer tools + orchestrator tool (build and test in isolation)**

Build `tools/claude_writer.py`:
- `write_section(...) → WriterDraft` — draft text + self-assessed `items: list[CritiqueItem]`
- Word target = `clamp(source_word_count, budget.min_words, budget.max_words)` (D-27/F-13)
- Severity definitions, score anchors, final-iteration signal, rejected_suggestions
  forwarding all in prompt
- Schema-validated + retry once (R-09)

Build `tools/gpt_writer.py`:
- Same interface as `claude_writer.py`
- OpenAI strict `json_schema` (severity enum enforced server-side — F-14)
- Length-budget violations appended deterministically in code, not left to GPT (F-14)
- Score anchors: "9–10 = publication-ready; 7–8 = one gap remains; 5–6 = multiple
  structural issues; 3–4 = weak draft" (validated on real data F-14: weak 3.0, strong 8.0)
- Schema-validated + retry once

Build `tools/orchestrator_tool.py` (Claude Sonnet, orchestrator role):
- `adjudicate(section_id, claude_draft, gpt_draft, rubric, jd, prior_scores) → OrchestratorDecision`
- Same score anchors as writer prompts (scores must be on same scale)
- Comparison prompt content-anchored: "evaluate against rubric and JD only —
  do not prefer one writing style over another" (prevents Claude-favours-Claude bias)
- Explicit tiebreak rule: "if scores within 0.5, prefer synthesis"
- Schema-validated + retry once

Build `tools/rubric.py`: rubric update + JD-validation logic. Test in isolation.

Test all tools with a real section + real JD (Airwallex) before wiring.
Use a gitignored driver (`tmp/step5_live.py`) for live API tests;
keep `tests/test_phases.py` mocked and API-free.

*Verification:*
- [ ] Orchestrator selects the better of two meaningfully different drafts with coherent direction
- [ ] `claude_quality` ≠ `gpt_quality` (scores discriminate)
- [ ] Weak draft → major CritiqueItem in `WriterDraft.items`; strong → minor or none
- [ ] `WriterDraft.pushback` is str when writer disagrees; None otherwise
- [ ] All tools schema-validated and retry on failure
- [ ] `test_schemas.py` green with `WriterDraft`, `OrchestratorDecision`, `LoopMemory` added

**Step 6 — Refinement loop (dual-writer, section-granular)**
Build `phases/phase3_refinement.py`.

Wire per-iteration loop:
1. Dual write: `claude_writer` + `gpt_writer` for all active sections
2. Orchestrator adjudication: `orchestrator_tool` per section → `OrchestratorDecision`
3. Write selected text to `<section_id>_v<n>.md`; per-writer drafts to `<section_id>_<writer>_v<n>.md`
4. Pushback exchange (if not final iteration)
5. Score: `IterationScore` (union keyword coverage + mean selected-draft quality)
6. Freeze sections where `converged=True`; update `LoopMemory`
7. Log `ReasoningEntry` for all decisions
8. Check termination conditions

Build with `max_iterations=1` first. Add prompt cache breakpoints after prompts
are stable — not during tuning. Convergence thresholds confirmed (F-16) — no change.

*Verification:*
- [ ] Single-iteration: valid `OrchestratorDecision` per section; disk files written correctly
- [ ] Multi-iteration: `quality_delta` decreasing or plateauing; convergence fires correctly
- [ ] Frozen sections excluded from iteration 2 dual-write calls
- [ ] `LoopMemory.rejected_suggestions` accumulates; `orchestrator_directions` grows by one/iter
- [ ] `run_log.jsonl` complete and readable after a full demo-mode run

**Step 7 — HITL, validation, and output generation**
Build `phases/phase4_hitl.py` (section status display), `phases/phase5_validation.py`
(per-section formatting + assembled length check), `phases/phase6_output.py`
(section assembly by position, per-section diffs in Changes tab).

*Verification:* HITL shows correct section status (converged/active/static);
Changes tab shows per-section version diffs; assembled CV word count within budget.

**Step 8 — Pipeline assembly and CLI**
Build `tailor/tailor.py` and `tailor/__main__.py`.
Wire all phases. Test full end-to-end in demo mode (1 iteration), then full mode.

*Verification:* `python -m tailor run --jd sample.txt --demo` completes;
all output files exist; cost breakdown accurate.

**Step 9 — Tests and hardening**
Add `tests/test_phases.py` with mocked LLM responses. Verify cost tracking
accurate per model. Verify `replay` command works. Verify section freeze logic
is deterministic (same input → same freeze decision in tests).

*Verification:* `pytest tests/` passes; cost breakdown matches estimated spend.

**Stretch — docx output** *(implemented, F-33)*
`--docx` flag → `cv_final.docx`, clean CV only. Respects the source CV's formatting
by **harvesting its conventions** (body font/size, name size, heading size/bold via
the table-aware `corpus.docx_loader`) and rendering the *same* assembled markdown as
`cv_final.md` into styled Word paragraphs (`tailor/phases/phase6_docx.py`) — not an
in-place clone (the tailored CV mixes/reorders sections from several CVs, D-17).
Deterministic; unit-tested against a fixture .docx.

---

## 9. Output Format

### cv_final.html

Four-tab interface:

**CV tab** — clean, printable. No reasoning, no annotations. This is the submittable artefact.

**Changes tab** — word-level diff between the starting CV and the final CV. Additions in green, removals in red. Shows specifically what changed across all iterations combined.

**Scores tab** — keyword coverage and critique score per iteration, rendered as a simple progression table. Unresolved critique items listed at the bottom.

**Reasoning tab** — collapsible audit trail, one entry per `ReasoningEntry` log item. Grouped by phase. "Why did the orchestrator reject this suggestion?" is answerable from here.

### cv_final.md

Clean CV only. This is what gets pasted into an email or a recruiter portal. No metadata, no annotations, no reasoning.

### run_log.jsonl

Machine-readable. One JSON object per line. Same format as Week 2 session transcripts. Used by `python -m tailor replay <run_id>`.

### Cost breakdown (in run_log.jsonl footer)

Model-level (D-08) and explicitly **estimated** list-price, never billed (F-08).
Keys match `tailor/cost.py`'s `CostTracker.footer` (the breakdown is keyed by
model: `anthropic_sonnet`/`anthropic_haiku`/`openai_gpt4o_mini`/`mistral_small`):

```json
{
  "type": "run_complete",
  "cost_breakdown_estimated_usd": {
    "anthropic_haiku": 0.1023,
    "mistral_small": 0.0003,
    "openai_gpt4o_mini": 0.0022
  },
  "total_estimated_usd": 0.1045,
  "total_estimated_gbp": 0.0826,
  "mode": "demo",
  "iterations_run": 1,
  "note": "list-price estimate, not billed; Mistral runs free-tier (F-08)"
}
```

---

## 10. Reuse from Previous Projects

### Week 1 RAG pipeline
- **Mistral embeddings client** — same API wrapper, new collection name
- **ChromaDB setup** — same persistence pattern, metadata filtering is additive
- **Chunking strategy** — CV sections are natural chunk boundaries; no fixed-size splitting needed

### Week 2 Finance Agent
- **`call_with_retry()` pattern** — `helpers.py` adapts this for three providers
- **JSONL transcript/audit logging** — same format, same structure, `audit.py` is a direct adaptation
- **Preview-before-apply HITL pattern** — Phase 4 is the same pattern: show the human what changed, ask to confirm
- **Mode switching** — demo vs full maps directly to the synthetic vs real data switch
- **Renderer protocol** — not reused directly (CLI output here is simpler) but the principle (separate display from logic) informs the HITL phase design
- **Checkpoint pattern** — each phase saves its output to disk; the pipeline can be inspected at any stage

### New patterns (not in Weeks 1–2)
- LLMs as tools (cross-provider orchestration)
- Dynamic evaluation rubric
- Dual-signal convergence
- Multi-provider cost tracking
- Document generation as output (HTML + markdown)
- Natural language as HITL interface (Haiku interprets free-text into structured pipeline decisions)
- Section-level composition recommendation (fit assessment across multiple CV versions)

---

## 11. Out of Scope

- Multi-user support or auth — personal tool, key-gated for full mode
- Automated CV submission — out of scope by design (the human submits)
- Automatic CV version management — the corpus is managed manually, ingested via CLI
- CVCM generation — the system consumes but never generates `value_creation_model.md`;
  a generation script (from CVs + reflections) is a future standalone tool, not part of this build
- Fine-tuning any model — documented as the next learning gap in the track, not this project
- LangGraph or any orchestration framework — same reasoning as Week 2 §3.2: build it manually first

---

## 12. Web UI Spec (Phase 2 — portfolio demo surface)

**Status:** Planned. Build after pipeline is complete and tested end-to-end.
**Stack:** FastAPI + React + SSE (same pattern as RFI Answer Builder)
**Goal:** Live demo surface for portfolio. Anyone watching should be able to
understand what the system is doing without reading the code.

---

### 12.1 — Two modes, same pattern as RFI

**Mode 1 — Corpus Management**
Ingest CVs, view what's in the corpus (section inventory per CV), delete/replace.
Direct reuse of the RFI ingestion UI pattern.

```
Landing page
  ├── Corpus stats (N CVs, M sections, last ingested date)
  ├── CV list with section breakdown per CV
  ├── [Add CV] → upload .docx → ingestion progress via SSE → section inventory confirmation
  └── [Delete CV] → removes all sections for that CV from ChromaDB
```

**Mode 2 — Tailoring Run**
Paste a JD, watch the pipeline run, handle HITL checkpoints conversationally,
download the final CV.

```
Run page
  ├── JD input (textarea + [Start] button)
  ├── Progress feed (SSE stream — one event per phase)
  ├── HITL checkpoint panels (appear inline as pipeline pauses)
  └── Output panel (final CV + scores + reasoning trace tabs)
```

---

### 12.2 — SSE progress events

One event per phase transition. Frontend renders a progress timeline:

```
● Phase 0  JD Analysis          complete  (0.4s)
● Phase 1  Fit Assessment        complete  (2.1s)  ← HITL pause here
● Phase 2  Initial Draft         complete  (8.3s)
● Phase 3  Refinement            running   iteration 2/3...
  ○ Phase 4  Human Review        waiting
  ○ Phase 5  Formatting          waiting
  ○ Phase 6  Output Generation   waiting
```

Event format (reuses RFI SSE pattern):
```json
{"type": "phase_start", "phase": "phase3_refinement", "iteration": 2}
{"type": "section_update", "section_id": "experience_acme", "status": "converged", "version": 3}
{"type": "phase_complete", "phase": "phase1_fit_assessment", "hitl_required": true}
{"type": "hitl_ready", "checkpoint": "fit_assessment", "payload": {...FitAssessment...}}
{"type": "run_complete", "run_id": "...", "cost_total_usd": 0.048}
```

---

### 12.3 — Conversational HITL panels

Each HITL checkpoint renders as a chat-style panel inline in the progress feed.
The pipeline is paused; the SSE connection stays open. On human response, the
frontend POSTs to `/api/runs/{run_id}/hitl` and the pipeline resumes.

**Checkpoint 1 — Fit Assessment (Phase 1)**

cv-tailor message:

> Here's the fit assessment for **[role title]** at **[company]**.
>
> **Recommended section mix** (74% overall coverage):
> | Section | Source CV | Coverage |
> |---|---|---|
> | Profile | solution_architect_generic | 81% |
> | Skills | solution_architect_ai | 88% |
> | Experience (Acme) | solution_architect_generic | 76% |
> | AI Projects | solution_architect_ai | only version with this section |
>
> **Transferable:** cloud architecture, stakeholder management, delivery at scale
>
> **Gaps:**
> ⚠ Kubernetes experience — major, addressable via tailoring
> ⚠ P&L ownership — major, addressable via tailoring
>
> Want to proceed with this mix, adjust any section source, or stop here?

For `no_fit` outcome:

> ⛔ **Significant gap — tailoring unlikely to resolve.**
>
> This JD requires **active SC clearance** (listed as non-negotiable). None of
> your CV versions mention this. CV tailoring cannot add a credential you don't hold.
>
> Proceed anyway (not recommended) or stop here?

Human response: free-text input. A Haiku call interprets the response into a
structured decision. The interpretation result is shown back to the human
before the pipeline resumes ("Got it — proceeding with your Skills section
swapped to the generic CV.").

**Checkpoint 2 — Section Review (Phase 4)**

cv-tailor message summarises section status and presents unresolved items with
explicit lettered options (a, b, c, d, e=free text). Same escape hatch pattern
as the CLI. Haiku interprets free-text; Sonnet executes if revision is needed.

**Checkpoint 3 — Formatting Validation (Phase 5)**

cv-tailor message shows the diff. Two buttons only: **Approve** / **Reject**.
No free-text. Haiku not needed — binary decision maps directly to pipeline state.

**Decision protocol (implemented, F-31).** Each `hitl_ready` event carries a
`checkpoint` + a JSON `payload`; the human's reply is `POST /api/runs/{id}/hitl`
with a single action dict. `SSEHITL` (api/runner.py) runs these on the paused
pipeline thread — the same handler interface as the CLI's `TerminalHITL`:

- `fit_assessment`: `{action: proceed|override|stop}` or `{action: freetext, text}`
  (Haiku → proceed/stop, shown back via a `hitl_interpreted` event before resuming).
- `section_review` (multi-turn loop): `{action: accept}` to proceed; `{action:
  apply_item, index}` to apply an unresolved item; `{action: interpret, text}` →
  Haiku interpretation re-published as `payload.preview` (preview-before-apply,
  D-18) → `{action: apply_freetext, section_id, instruction}` to confirm.
- `formatting`: `{action: approve|reject}` (binary; no LLM).

`POST /api/runs` also takes `auto: bool` (default **false** = conversational HITL).
`auto:true` uses `AutoHITL` for the start-to-finish demo path (F-31).

---

### 12.4 — Output panel

Four tabs (same as the HTML output file, rendered inline):

```
[CV]         Clean assembled CV — copy-to-clipboard button
[Changes]    Per-section version diffs
[Scores]     Keyword coverage + critique score per iteration per section
[Reasoning]  Collapsible audit trail by phase
```

Download buttons: `cv_final.md` (clean text) and `cv_final.html` (full report).

---

### 12.5 — Architecture

Same shape as RFI:
```
Browser
  └── frontend:3000 (React + Vite + shadcn/ui)
        └── /api/* → backend:8000 (FastAPI + SSE)
              └── import (not subprocess) → tailor package
                    └── ChromaDB + Mistral + Anthropic + OpenAI
```

Key decisions (to be confirmed during UI build, using RFI learnings as defaults):
- Import pipeline functions, never subprocess-shell (RFI entry 15/16)
- Filesystem-backed sessions with TTL (RFI entry 16) — one `tmp/{run_id}/` per run
- SSE connection stays open during HITL pauses; pipeline resumes on POST to `/hitl`
- `asyncio.to_thread` for all pipeline calls (blocking I/O off the event loop)
- `restart: unless-stopped` in production compose (RFI entry 26)

---

### 12.6 — UI build sequence (post-pipeline)

**UI Step 1 — FastAPI scaffold + session management**
Stand up `api/` with FastAPI, stub the three routers (corpus / runs / hitl),
implement session management. Add `backend` Docker Compose service. No pipeline
wiring yet.

**UI Step 2 — Corpus management UI**
`/api/corpus/*` endpoints wrapping `corpus.ingest` and `corpus.retrieval`.
Frontend: landing page with corpus stats, CV list, upload flow with SSE progress,
delete. Direct reuse of RFI ingestion UI pattern.

**UI Step 3 — Run initiation + SSE progress feed**
`POST /api/runs` starts a run in a background thread. `GET /api/runs/{id}/stream`
SSE endpoint. Frontend: JD input, progress timeline rendering SSE events.
Pipeline runs to first HITL checkpoint then pauses.

**UI Step 4 — Conversational HITL**
`POST /api/runs/{id}/hitl` receives human response. Haiku interpretation call.
Structured decision returned to pipeline thread to resume. Frontend: chat-style
checkpoint panel, response input, confirmation of interpretation before resume.

**UI Step 5 — Output panel + downloads**
Render final CV tabs inline. Download endpoints for `cv_final.md` and
`cv_final.html`. Cost breakdown display.

**UI Step 6 — Production compose + polish**
Multi-stage frontend build (nginx-alpine). `docker-compose.prod.yml` overlay.
Demo hardening: error states, loading indicators, empty corpus state.
