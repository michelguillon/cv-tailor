# cv-tailor_SPEC.md — cv-tailor
## Architecture Specification

> **Historical design document.**  
> This file reflects the original design intent, agreed before the build began.  
> The product evolved significantly during implementation — a web UI, security gates,  
> run management, and several architectural refinements (dual-writer loop, section-granular  
> ingestion, CVCM) were added after this spec was written.  
> For the deployed implementation, see **cv-tailor_ARCHITECTURE.md**.  
> For the full decision trail including build findings, see **cv-tailor_LEARNING_NOTES.md**.

**Project:** Week 3 Portfolio — Multi-Model Orchestration  
**Repository:** cv-tailor
**Status:** Complete — pipeline + UI + Langfuse tracing + SSE resilience deployed  
**Last updated:** June 2026 — project closed (F-55 SSE reconnect; F-53/F-54 Langfuse; F-52 Job Radar callback)  
**Deployment target:** CLI tool + web UI, M720q home server

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
| Anthropic | claude-sonnet-4-6 | Orchestrator + primary writer + revision decisions | Complex multi-step reasoning. Cross-turn consistency. Established from Week 2. |
| OpenAI | gpt-4o-mini | Independent challenger writer | Empirically observed: produces harsher, more direct drafts than Claude. Less likely to flatter. Different training prior = genuinely independent second perspective. (D-28) |
| Anthropic | claude-haiku-4-5 | Formatting validation + HITL interpretation + demo orchestrator | Fast, cheap, sufficient for deterministic checks. ~20× cheaper than Sonnet. Demo mode orchestrator. |

*Updated post-D-28: GPT-4o-mini is a writer (not just a critic). Both models draft independently per section; Claude Sonnet orchestrates adjudication.*

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

**How the key is enforced.** `resolve_run_config` reads `FULL_MODE_KEY` from the **environment** directly (the `full_mode_key: "${FULL_MODE_KEY}"` line above is documentation — YAML does not expand env vars). Full mode requires the env var to be set **and** the supplied key to match, else `ConfigError`. The **CLI** passes the key via `--key`. The **Web UI** moves to a one-time unlock that issues a signed capability cookie instead of sending the raw key per run — see **§12.7 (planned, D-38)**; the cookie becomes the Web gate while `--key` stays the CLI gate.

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

### 3.10 — CLI-first, HTML output IS the review interface

> **Superseded.** A full React/FastAPI web UI was built and deployed (§12).  
> This section reflects the original design intent only.

**Decision (original):** No web UI. The HTML output file serves as the review interface. One-time ingestion runs as a CLI command. The tailoring run is a CLI command with HITL prompts in the terminal.

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
    keyword_coverage: float       # SOURCE-GROUNDED UNION coverage across non-static
                                  #   sections (F-15 + F-38): the CV-level "fraction of
                                  #   the rubric covered anywhere AND supported by the raw
                                  #   corpus" — an inserted keyword the source can't back
                                  #   counts 0 (the Goodhart fix). (Earlier draft said
                                  #   "weighted mean"; union matches F-11. Absolute values
                                  #   are lower post-F-38 — they exclude fabricated coverage.)
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
    structure_preserved: bool    # F-56: did the draft keep the SOURCE's list shape (bulleted
                                 # experience stays bullets; a "·"-delimited skills list stays a
                                 # list)? Set in CODE by the writer (deterministic marker count,
                                 # not model self-report); the orchestrator treats False as a
                                 # selection disqualifier so a draft flattened to prose can't be
                                 # chosen or frozen. Defaults True (prose sources are unconstrained)

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
    jd_raw: str                  # raw JD text as submitted; stored for traceability
                                 # rendered verbatim in the JD tab of cv_final.html
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
  Each writer also sets WriterDraft.structure_preserved (F-56): a deterministic,
    code-side check that the draft kept the source's list shape (bulleted experience
    stays bullets; a "·"-delimited skills list stays a list). Writers are told to
    match the source's structure (STRUCTURE_RULES, ahead of the content guidance in
    both system prompts); the flag is the enforcement, not the model's word.

Step 2 — Orchestrator adjudication (Claude Sonnet, orchestrator role)
  Sees: both WriterDrafts (text + items + structure_preserved), rubric, keyword scores
  A draft with structure_preserved=False (flattened the source to prose) is
    DISQUALIFIED as the selected base; if both lost structure, converged is forced
    False with a direction to restore the bullet/list format (F-56)
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
# docker-compose.prod.yml — homeserver overlay (Caddy-fronted; PLAYBOOK.md)
services:
  backend:
    container_name: cv-tailor-backend          # nginx targets this by name (gotcha #6)
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000  # no --reload
    restart: unless-stopped
    ports: !override []                        # internal-only (reached via default net)

  frontend:
    container_name: cv-tailor-frontend         # Caddy: reverse_proxy cv-tailor-frontend:3000
    image: cv-tailor-frontend-prod  # distinct name so a prod build doesn't clobber
                                     # the dev Vite image (same default name) — F-32
    build:
      context: ./frontend
      dockerfile: Dockerfile.prod   # multi-stage: Node build → nginx-alpine (~50 MB)
    restart: unless-stopped
    ports: !override []             # Caddy reaches :3000 over the shared caddy network
    networks: [default, caddy]      # default = reach the backend; caddy = ingress

  backend:
    networks: [default, tracing]    # tracing = reach langfuse-langfuse-web-1 directly
                                    # NOT on caddy: Caddy only needs the frontend (D-41)

networks:
  caddy:   { external: true }       # one-time: docker network create caddy
  tracing: { external: true }       # one-time: docker network create tracing
                                    # shared with langfuse compose stack
  default: {}
```

The entry point is the **frontend nginx** (`cv-tailor-frontend:3000`), which proxies
`/api/*` → `cv-tailor-backend:8000` over the per-app `default` network; only the frontend
joins the shared `caddy` network (addressing siblings by container_name, never bare
service alias — PLAYBOOK gotcha #6). Caddy (port 80, behind a Cloudflare Tunnel) does the
TLS-less reverse proxy. Full step-by-step in **`DEPLOY-cv-tailor.md`** (caddy-stack).

**Deployment to homeserver (M720q):**

```bash
# 1. Clone repo on server (networks must exist: docker network create caddy && docker network create tracing)
git clone <repo> /opt/apps/cv-tailor && cd /opt/apps/cv-tailor

# 2. Set API keys and optional Langfuse config
cp .env.example .env && $EDITOR .env
# Required: MISTRAL_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, FULL_MODE_KEY
# Optional Langfuse tracing (leave blank to disable cleanly):
#   LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
#   LANGFUSE_BASE_URL=http://langfuse-langfuse-web-1:3000  ← internal Docker addr, not public URL
#   Also: docker network create tracing (if Langfuse is deployed)

# 3. Seed the corpus WITHOUT git: scp the source CVs (+ .yaml sidecars) from the dev box,
#    then re-embed on the server (data/chroma + budgets.yaml are written there, persisted
#    by the backend bind-mount). The .docx/.yaml are gitignored, so they travel out-of-band.
mkdir -p data/cvs
scp -P <ssh-port> -r "<dev>/data/cvs/." user@server:/opt/apps/cv-tailor/data/cvs/
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    run --rm cli python -m corpus.ingest --cv-dir data/cvs/      # confirm the inventory gate

# 4. Build + start (Caddy + Cloudflare ingress per DEPLOY-cv-tailor.md)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backend frontend
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
Post-F-38, the Phase-3 calls pass `source_text` so coverage is **source-grounded** (an
inserted keyword the candidate's corpus can't back scores 0 — the Goodhart fix); Phase 1
scores the raw corpus, so it passes no source and is unchanged.

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

Seven-tab interface (Fit · CV · Grounding · Changes · Scores · Reasoning · JD):

**Sticky summary card (all tabs)**

A fixed header card visible on every tab of `cv_final.html` — does not scroll
away, always readable without switching tabs. Populated from `PipelineOutput`
at Phase 6 render time.

```
┌─────────────────────────────────────────────────────────┐
│  🟡 Fit: Partial (58%)                                  │
│                                                         │
│  ✓  Grounded Coverage:   36%                            │
│  ⚠  Unsupported Claims:  1                              │
│  Status: Review Required                                │
│                                                         │
│  Run: run_20260606_114928                               │
└─────────────────────────────────────────────────────────┘
```

Fit indicator colours: 🟢 Strong (≥75%) · 🟡 Partial (40–74%) · 🔴 No Fit / Review (<40%)

Fields:
- `Fit` — outcome label + `overall_fit_score` as percentage
- `Grounded Coverage` — the final iteration's **source-grounded** `keyword_coverage`
  (F-38): the fraction of JD/rubric keywords the CV covers *and* the candidate's raw
  source evidences (an unsupported keyword earns no coverage)
- `Unsupported Claims` — the **verifier's** `fabrication_flags` (F-35): count of claims
  in the final CV not supported by the candidate's own source CV (same number as the
  Grounding tab)
- `Status` — derived: "Submit-ready" if fit ≥75% + 0 unsupported claims; "Review
  Required" if fit <75% or any unsupported claims; "Do Not Submit" if no_fit
- `Run` — `run_id` for traceability

Note (F-43): the card is sourced from signals the pipeline **already** produces — there
is **no** extra Phase-5 grounding pass and no added LLM spend. `phase6_output.summary_card()`
is the single source of truth for the band + status (reused by `api/archive.py` so the web
card and HTML card can't drift); `grounded_coverage` + `fabrication_flags` ride the
`run_complete` footer.

**Fit tab** (F-39, default-active) — role-fit summary: the CVCM value-alignment narrative ("why am I a fit", D-33), transferable strengths, and gaps. Visible after any run including `--yes`/auto (which never pauses at the Phase-1 checkpoint). Falls back to the no-fit reason when there's no CVCM.

**CV tab** — clean, printable. No reasoning, no annotations. This is the submittable artefact.

**Grounding tab** (F-35) — the verifier's unsupported-claim flags: claims in the final CV the candidate's own source corpus doesn't support, raised at the review step. All-clear when zero. The summary card's `Unsupported Claims` count is this same number.

**Changes tab** — word-level diff between the starting CV and the final CV. Additions in green, removals in red. Shows specifically what changed across all iterations combined.

**Scores tab** — keyword coverage and critique score per iteration, rendered as a simple progression table. Unresolved critique items listed at the bottom.

**Reasoning tab** — collapsible audit trail, one entry per `ReasoningEntry` log item. Grouped by phase. "Why did the orchestrator reject this suggestion?" is answerable from here.

**JD tab** (D-37) — the raw job description exactly as pasted. No analysis, no annotations, no highlighting. Purpose: traceability — knowing which role a run was for without needing to open an external file. Persisted to `outputs/<run_id>/jd_raw.txt` (and `PipelineOutput.jd_raw`), rendered verbatim. Empty-state for pre-feature runs that didn't record it.

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

**Default tab on app load: Run** (not Corpus). The primary use case is starting
a tailoring run. Corpus management is secondary — accessed via a nav tab, not
the landing screen.

---

**Mode 1 — Run (default)**
Paste a JD, watch the pipeline run, handle HITL checkpoints conversationally,
download the final CV.

```
Run page
  ├── JD input (textarea + [Start] button)
  ├── Progress feed (SSE stream — one event per phase)
  ├── HITL checkpoint panels (appear inline as pipeline pauses)
  └── Output panel (sticky summary card + tabs: Fit · CV · Changes · Scores · Reasoning · JD)
```

---

**Mode 2 — Corpus Management**
View, add, update, and delete CVs in the corpus.

```
Corpus page
  ├── Corpus stats (N CVs, M sections, last ingested date)
  ├── [+ Add CV] (header) ; empty corpus → prominent [+ Add CV]
  ├── CV list — one row per CV, showing:
  │     display_name | target_role | seniority | N sections
  │     [Sections] [Edit] [Replace] [Delete] buttons per row
  │
  ├── [Add CV] flow (wizard):
  │     Step 1 — Upload .docx file (client-side duplicate check → "use Replace")
  │     Step 2 — YAML metadata form (filename read-only from the upload):
  │               cv_type, target_role, target_company,
  │               skills_emphasis (chip input), seniority, version_date
  │     Step 3 — Section inventory display (POST /upload, no writes yet) →
  │               human confirms the section list before committing to ChromaDB
  │               (load-bearing gate: silent parse failures caught here, R-01;
  │               ⚠ warning when section count < MIN_SECTIONS)
  │     Step 4 — [Confirm & Add] (POST /confirm) → embed + store; done
  │
  ├── [Edit Metadata] flow (per-row, one step):
  │     Form pre-filled from GET /cvs/{filename}/metadata → Save
  │     (PATCH /cvs/{filename}/metadata). Updates the sidecar AND patches the
  │     ChromaDB section metadata (the list + retrieval filters read it there,
  │     so a sidecar-only edit would be inert). No re-embedding, no inventory gate.
  │
  └── [Replace .docx] flow (per-row): same 4-step wizard as Add, pre-filled from
        existing metadata, new .docx stored under the existing filename. On confirm
        (POST /confirm, replace=true) deletes all existing ChromaDB entries for the
        filename, then re-ingests. De-duplication key: filename (D-10).
```

**Endpoints (all synchronous JSON — F-42).** `GET /stats`, `GET /cvs`,
`GET /cvs/{filename}/metadata`, `POST /upload`, `POST /replace`, `POST /confirm`,
`PATCH /cvs/{filename}/metadata`, `DELETE /cvs/{filename}`. Add/Replace are two HTTP
steps so the R-01 section-inventory gate sits before any ChromaDB write; the staged
`.docx` lives in `tmp/corpus/<token>/` and moves to `data/cvs/` only on confirm.
Ingest is **not** SSE — one CV is a single batched embed call and the human gate is
preview→confirm, not progress-watching (F-42). Error contract: **409** duplicate on
Add, **422** invalid metadata, **410** expired staged upload, **404** edit/delete of
an absent CV. `budgets.yaml` is re-derived from ChromaDB metadata after each confirm
(F-42, refines D-14). The three **read** endpoints (`GET /stats`, `/cvs`,
`/cvs/{filename}/metadata`) are public; the five **mutating** ones (`POST /upload`,
`/replace`, `/confirm`, `PATCH …/metadata`, `DELETE …`) require the owner capability
cookie (**§12.8 / D-39**) — 403 fail-closed otherwise.

**Why the YAML form rather than a sidecar file upload:**
In the CLI workflow, metadata lives in a `.yaml` sidecar file alongside the
`.docx`. In the UI, the form replaces the sidecar — the user fills it in
interactively rather than editing a YAML file. The backend writes the equivalent
sidecar to `data/cvs/<filename>.yaml` on confirm, keeping the CLI and UI
workflows compatible (both result in the same on-disk state).

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

Sticky summary card at the top (always visible regardless of active tab, D-34/F-43 —
sourced from existing signals, no extra LLM pass):
```
🟡 Fit: Partial (58%)  ·  ✓ Grounded Coverage: 36%  ·  ⚠ Unsupported Claims: 1
Status: Review Required  ·  Run: run_20260606_114928
```
The web OutputPanel renders this card from the run's archive summary
(`grounded_coverage`, `unsupported_claims`, `status`, `fit_band`); the embedded
`cv_final.html` iframe carries its own copy of the card + the JD tab.

Seven tabs rendered inline (matching `cv_final.html`):

```
[Fit]        Default-active. CVCM value-alignment narrative, transferable
             strengths, gaps. Falls back to no-fit reason if no CVCM.
[CV]         Clean assembled CV — copy-to-clipboard button
[Grounding]  Verifier's unsupported-claim flags (vs the candidate's source CV)
[Changes]    Per-section version diffs across all iterations
[Scores]     Keyword coverage + quality score per iteration per section
[Reasoning]  Collapsible audit trail by phase
[JD]         Raw job description as pasted — no analysis, no annotations
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

---

### 12.7 — Full Mode Unlock Gate (built, D-38 / F-44)

> **Status: built.** The Web path now gates full mode on a **one-time unlock that
> issues a signed, HttpOnly capability cookie** instead of sending the raw key in
> every full run's body. The key is entered once (the unlock dialog), exchanged for
> the cookie, and never stored in the browser. The **CLI is unchanged** — `--key`
> stays the CLI gate (no browser, no cookie). Implementation: `api/security.py`
> (stdlib-HMAC token signed with `FULL_MODE_KEY`), `api/routers/full_mode.py`
> (`GET /api/capabilities`, `POST /api/full-mode/unlock` + `/lock`), and the full-run
> gate in `api/routers/runs.py` (403, fail-closed).

**Objective.** A single publicly-deployed instance exposes low-cost **demo** mode to
visitors while restricting high-cost **full** (Sonnet) mode to authorised use. This is
**not** full authentication — it is a guard against accidental or public use of the
expensive mode, keeping the portfolio app simple and usable. Demo stays open and default;
full requires an unlock.

**Modes.**
- **Demo** — public, low-cost config, default, no unlock (the §3.7 `demo` mode).
- **Full** — restricted, higher-cost config (the §3.7 `full` mode), requires unlock.

**Unlock flow (Web).**
1. User selects Full mode. If not already unlocked, the UI shows an unlock prompt for
   the full-mode key (a password field — never persisted in React state, `localStorage`,
   or a readable cookie).
2. UI `POST`s the key to a new endpoint, e.g. `POST /api/full-mode/unlock {key}`.
3. Backend validates the key against `FULL_MODE_KEY`. On success it sets a **signed,
   HttpOnly capability cookie** (e.g. `cv_full=<signed token>`) and returns `{unlocked:true}`;
   on failure returns `401` and sets nothing (user stays in demo).
4. UI flips to "full unlocked"; subsequent full runs rely on the **server-issued cookie**
   (sent automatically by the browser), not repeated key entry.

**Backend enforcement (the source of truth).** A full-mode run (`POST /api/runs` with
`mode:"full"`) is **rejected with 403** unless **all** hold:
- full mode is configured server-side (`FULL_MODE_KEY` set), and
- the request carries a **valid, unexpired, signature-verified** capability cookie.
The run path stops reading a `key` from the request body for the Web flow; it checks the
cookie instead, then resolves the full `RunConfig`. **Fail closed:** missing/invalid
config or cookie ⇒ full is refused (demo only). UI hiding is convenience only and is
never relied on for protection.

**Capabilities endpoint.** A lightweight `GET /api/capabilities` (or an extension of
`/api/health`) returns, for the current request:
```json
{ "demo_available": true, "full_configured": true, "full_unlocked": false }
```
- `full_configured` — `FULL_MODE_KEY` is set server-side.
- `full_unlocked` — the request carries a valid capability cookie.
The UI uses this to choose: show full directly (configured + unlocked), show the unlock
prompt (configured + not unlocked), or **hide/disable full** (not configured) — which makes
a blank-key public deploy cleanly demo-only with no dead option.

**Capability cookie design.**
- Signed token (HMAC over an expiry/nonce with a server secret — e.g. `itsdangerous`),
  proving "unlocked until `<exp>`". The raw key is **not** stored in the cookie.
- Attributes: `HttpOnly`; `SameSite=Lax`; `Secure` in production (behind Cloudflare TLS);
  `Path=/api`; an expiry (`Max-Age`) chosen for owner convenience vs. exposure (hours–days).
- Server secret from env (a dedicated signing secret, or derived from `FULL_MODE_KEY` so
  rotating the key invalidates outstanding cookies). Expiry-based invalidation; an optional
  `POST /api/full-mode/lock` clears the cookie.

**UX requirements.** Demo is the default for public users; selecting full triggers the
unlock flow only if needed; once unlocked, no re-entry for the valid session; a failed
unlock keeps the user in demo; full is clearly labelled **higher-cost / restricted**; the
selected mode is visible on the run screen **before** execution.

**Safety requirements.** No raw key client-side; no key sent per run; no frontend-only
enablement; signed server-issued cookie; secure cookie settings in prod; full mode fails
closed when configuration is missing or invalid.

**Optional cost controls (future, before wider sharing).** Full mode may also enforce
operational caps — max full-mode runs/day, max estimated full-mode spend/day, max
concurrent full-mode runs, with a clear warning at the cap. These build on the per-mode
`cost_cap_usd` (§3.7/D-08) — note that today the cap is **reported** after a run, not a
hard mid-run stop, so enforcement would add a hard stop + in-memory daily counters (same
shape as a per-IP rate limit). Optional for the initial implementation.

**Success criteria.** Public visitors use demo with no setup; full cannot be triggered by
unauthorised users; the owner unlocks full with one key entry; the key is never persisted
in the browser; backend enforcement prevents bypassing the UI; the app stays a single
deployed instance with no full auth.

**Impact when implemented** (for the build): backend — new `unlock` / `capabilities`
(+ optional `lock`) endpoints, a cookie sign/verify helper, `start_run` changed to gate
full on the cookie (not the body key), a new signing-secret env var; frontend — fetch
capabilities, an unlock dialog, mode state driven by capabilities, drop the raw-key-per-run;
tests — unlock success/fail, 403 on a full run without a valid cookie, capabilities states,
cookie expiry/signature. The CLI `--key` path and `resolve_run_config`'s key check are
retained for non-Web use.

---

### 12.8 — Gate Corpus Write Operations (built, D-39 / F-45)

> **Status: built.** The corpus stays **publicly browsable** (anyone can view the
> inventory, metadata, and section breakdowns), but every operation that **changes
> persisted corpus state** now requires the **same owner capability cookie** as full
> mode (§12.7). One unlock authorises both expensive runs *and* corpus edits — there is
> no second key, secret, or cookie. Implementation: a `require_unlocked` FastAPI
> dependency in `api/security.py` (reuses `verify_token`), attached to the five mutating
> `api/routers/corpus.py` endpoints; the frontend reuses the §12.7 unlock dialog via a
> shared `UnlockProvider`.

**Objective.** Keep the portfolio app a single public deployment that visitors can
*inspect* without authentication, while preventing anyone but the owner from *modifying*
the corpus. This is the §12.7 spend-guard model applied to write operations — **not** full
authentication, accounts, or roles.

**Access model.**
- **Public (no unlock)** — view corpus inventory + metadata, see CV archetypes / source
  assets, and use demo mode. All read-only `GET` corpus endpoints stay open.
- **Unlocked (valid capability cookie)** — add / replace / delete CVs, trigger metadata
  extraction / re-indexing, update sidecar metadata, *and* use full mode (the same
  capability state, §12.7). One unlock, both powers.

**Unlock behaviour (Web).** Identical flow to full mode:
1. The user clicks Add / Replace / Delete (or any corpus-mutating action).
2. If the session is not unlocked, the UI opens the **same unlock prompt** (owner key,
   password field — never persisted in React state, `localStorage`, or a readable cookie).
3. The backend validates the key and sets the signed HttpOnly capability cookie (§12.7).
4. The original corpus action proceeds after a successful unlock; the raw key is not
   stored client-side or re-sent per action.

**Backend enforcement (the source of truth).** Each corpus-mutating endpoint is gated by a
`require_unlocked` dependency and **rejected with 403** unless **both** hold: an owner key
is configured server-side (`FULL_MODE_KEY` set) *and* the request carries a valid,
unexpired, signature-verified capability cookie. **Fail closed:** no key configured ⇒ the
deployment is **read-only** (every write 403s, even for the owner — set the key to enable
writes); missing/invalid cookie ⇒ refused. Protected operations: add document, replace
document, delete document, re-index, update metadata / sidecar, **and any future endpoint
that mutates corpus state**. Read-only corpus endpoints remain public. The dependency runs
before the handler, so a refused write never parses the CV, embeds, indexes, or writes a
file (Starlette may buffer the multipart body to its own temp first, but that is request
plumbing, not corpus state — no `tmp/corpus/` staging, no `data/cvs/` write, no ChromaDB
call happens). UI hiding/disabling is convenience only and is never relied on for
protection — a direct `curl` to a mutating endpoint without the cookie is refused.

**UI behaviour.** The corpus page stays visible to everyone.
- **Locked but unlock available** (key configured, no cookie): write controls remain
  visible; clicking one opens the unlock prompt; a status line makes clear *viewing is
  public, editing requires owner unlock*.
- **Unlocked**: write actions are enabled, a small "owner unlocked" indicator (with a
  *lock* affordance) is shown, and the valid session is not re-prompted.
- **Read-only deployment** (no key configured server-side): write controls are hidden and
  a note states the corpus is view-only here — mirroring how full mode is hidden when not
  configured (§12.7), so there is no dead/always-failing button.

**Failure behaviour.** A failed unlock keeps the user on the corpus page, performs no
action, and shows a clear "editing requires owner access" message. A write request that
reaches the backend without a valid unlock returns 403 and does **not** partially mutate
corpus state (no parse, no index, no file write).

**Success criteria.** Visitors inspect the corpus without authentication; visitors cannot
modify it; Add / Replace / Delete (and re-index / metadata edits) require owner unlock;
backend protection cannot be bypassed via direct API calls; the **same** unlock mechanism
protects both expensive model runs and corpus writes; the app stays a single public
deployment with no full authentication.

**CLI is unaffected.** `corpus.ingest` writes the corpus directly (no browser, no cookie,
no API) — the gate is purely the Web write path, exactly as `--key` remains the CLI gate
for full mode (§12.7).

---

### 12.9 — Run Visibility and Retention Controls (built, D-40 / F-46)

> **Status: built.** A public deployment must not expose every working run. The Runs
> archive is now **capability-aware**: public visitors see only runs explicitly marked
> **public demo**; the owner (valid capability cookie, same as §12.7/§12.8) sees all runs
> with management controls. Visibility (`public_demo`) and retention (`keep`) are mutable
> per-run flags stored in a sidecar (`outputs/<run_id>/run_meta.json`), **separate from
> the model/cost `mode`**. Stale private runs are auto-cleaned (env-gated) or cleaned on
> demand. Implementation: `api/run_meta.py` (sidecar), `api/archive.py` (filter + delete +
> cleanup), `api/routers/runs.py` (capability-aware list/view + `PATCH /{id}/meta`,
> `DELETE /{id}`, `POST /cleanup` — all `require_unlocked`), startup cleanup in `api/main.py`.

**Objective.** Keep a small curated public demo surface (a few showcase runs a recruiter can
open) while keeping ordinary working runs private to the owner — without full authentication.
Same spend-guard philosophy as §12.7/§12.8, applied to *run exposure*.

**Access model.**
- **Public (no unlock)** — list only `public_demo` runs; open only those runs' reports;
  use demo mode. Public visitors may **not** list all runs, see private/unreviewed runs,
  delete runs, change `keep`/`public_demo`, or open a full-mode run unless it is marked
  public demo. The public list is **redacted** (no cost, created_at, or unsupported-claim
  internals — "full metadata" is owner-only).
- **Unlocked (valid capability cookie)** — list all runs with full metadata; open any run;
  delete; mark/unmark `keep`; mark/unmark `public_demo`; edit run metadata (e.g. company).

**Run metadata.** Each run carries enough to be understandable without opening the report:
`run_id`, `created_at`, `company_name`, `role_title`, `fit_label`, `fit_score`, `mode`,
`iteration_count`, `estimated_cost`, `unsupported_claim_count`, `keep`, `public_demo`.
`role_title`/fit/cost/grounding come from the existing run record (Phase-0/1 checkpoints +
the `run_complete` footer + `summary_card`, §12.4/F-43). `company_name`, `keep`, and
`public_demo` live in a **mutable sidecar** `run_meta.json` (the append-only `run_log.jsonl`
audit is never mutated, D-06). `company_name` resolves by precedence (**F-47**): the owner's
**manual** value (an optional field on the run form, editable later) wins; else the name
**inferred from the JD** by Phase 0 (`JDAnalysis.company_name`, extracted in the existing
analysis call — no extra LLM pass); else **null**, where the UI shows **"Unknown company"**.
The manual value always overrides inference (an LLM can grab a stray subsidiary/brand).
`created_at` is the run id's timestamp (`run_YYYYMMDD_HHMMSS`, UTC).

**Visibility ≠ mode ≠ retention (three orthogonal concepts).**
- `mode` — model/cost config: `demo` or `full` (§3.7).
- `public_demo` — whether the run is visible to public visitors.
- `keep` — whether the run is protected from automatic cleanup.
A demo run is **not** automatically public; a full run defaults **private** but *can* be
marked public demo explicitly. **Defaults for every new run:** `public_demo = false`,
`keep = false`.

**Retention.** Automatic cleanup of stale private runs, rule:
*delete runs older than `RUN_RETENTION_DAYS` (default 7) unless `keep` or `public_demo`*.
Runs are deleted by **age from the run id timestamp** (immune to later sidecar writes).
Cleanup runs (a) on backend **startup** — only when `RUN_RETENTION_DAYS` is set (unset ⇒
no automatic deletion, so dev/test are never destructive), and (b) on demand via
`POST /api/runs/cleanup` (owner-only). A startup or manual action is sufficient for the
initial deployment; a cron is not required.

**Backend enforcement (the source of truth).**
- `GET /api/runs/archive` is **capability-aware**: returns only `public_demo` runs (redacted)
  unless the request carries a valid capability cookie, in which case it returns all runs
  with full metadata.
- `GET /api/runs/{id}/detail|report|files/*` **404** a run that is not `public_demo` when the
  request is not unlocked (don't reveal private run ids); public demo runs open for anyone.
- `PATCH /api/runs/{id}/meta` (set `company_name`/`keep`/`public_demo`), `DELETE /api/runs/{id}`,
  and `POST /api/runs/cleanup` require `require_unlocked` — **403 fail-closed**. UI hiding is
  convenience only; a direct `curl` without the cookie is refused / sees only public runs.

**UI behaviour.**
- **Public run list** — curated demo runs only: `Company — Role Title` / `Fit label · score ·
  mode · iterations` / **Open**.
- **Unlocked run list** — all runs + controls: `Company — Role` / `Fit label · score · mode ·
  iters · cost · created_at` / `[Open] [Keep] [Public Demo] [Delete]` + edit company; runs
  with grounding issues show **⚠ N unsupported claims**; a header lock indicator and a
  *Clean up old runs* action. On a deployment with no owner key, no management controls show
  (mirrors §12.7/§12.8).

**Success criteria.** Recruiters/visitors see only curated demo runs; working runs stay
private by default; the owner manages runs from the UI (no terminal); old unimportant runs
are cleaned automatically; important runs (`keep`) persist indefinitely; demo/full mode is
never conflated with public/private visibility; the list reads by company + role, not run ids.

**CLI is unaffected.** Runs still write to `outputs/<run_id>/` exactly as before; visibility
and retention are a Web-surface concern. A CLI/owner can still inspect every run on disk.

### 12.10 — Job Radar handoff (built, Integration §5.2 / F-51)

cv-tailor is the *how should I pursue this role?* half of a pair with **Job Radar** (*which roles
are worth pursuing?*). Phase 2 of their integration (`../job-radar/docs/INTEGRATION_SPEC_JR_CVT.md
§5`) lets a Job Radar role open straight into a pre-populated cv-tailor run. The pipeline (Phases
0–6), HITL, auth, and output formats are unchanged — this is purely an entry path + a stored link.

**URL handoff.** Job Radar's *Create CV in cv-tailor ↗* button opens
`…/new?source=job_radar&job_id=<job_id>`. On mount the Run page reads the query params,
**strips them** (`history.replaceState`, so a refresh doesn't re-trigger), and — when
`source=job_radar` — fetches the job to pre-fill the form.

**Server-side fetch (never the browser).** The JD is pulled from Job Radar's **public**
`GET {JOB_RADAR_API_URL}/api/jobs/{job_id}` server-side, in two places:
- **Prefill proxy** `GET /api/job-radar/jobs/{job_id}` (`api/routers/job_radar.py`) — display-only;
  the Run page calls *this*, not Job Radar, to pre-populate the JD textarea + company (avoids CORS).
- **Run start** `POST /api/runs` with `source=job_radar` + `job_id` (`api/routers/runs.py`) — the
  authoritative fetch: `raw_text` becomes the JD body, `company` seeds the run label, and the rest
  becomes the stored reference. The textarea is read-only once loaded (the JD is authoritative).

`JOB_RADAR_API_URL` (env, default `https://job-radar.michel-portfolio.co.uk`) allows local override.
No auth — the endpoint is public (Phase 2). Phase 3's callback (cv-tailor → Job Radar) will add a
service token later; not built here.

**Stored reference — `run_meta.json`, write-once.** A run from Job Radar carries
`job_radar_source: {job_id, company, title, source_url, fit_label, fit_score}` in the **mutable
sidecar** (`api/run_meta.py`), beside `public_demo`/`keep`/`company_name` (§12.9) — never in the
append-only `run_log.jsonl` (audit ≠ context, D-06). It's set at creation and never mutated (no
PATCH field); it returns in `GET /api/runs/{id}/detail` and `/archive`.

**Failure contract — fail loud, never silent.** Any fetch problem (network, 404, non-JSON, or empty
`raw_text`) → **502** from `POST /api/runs` and **no run is created** (the request aborts before
allocating a run id). A run is never started with an empty/placeholder JD. On a *proxy* failure the
Run page shows an inline "Could not load job from Job Radar — paste the JD manually", drops the
linkage (→ a normal run), and never blocks the page.

**Visibility.** Runs from Job Radar default `public_demo: false` (the existing §12.9 default — not
overridden). `job_radar_source` is **owner-only**: `source_url` points at a personal job-search
tool, so it's redacted from the public archive list and blanked in `GET /runs/{id}/detail` for any
locked request (even a public-demo run or a live-session viewer). The owner sees a small
*From Job Radar: Company — fit_label (score) ↗* line in the output panel, linking back to the role.

**Phase 3 — completion callback (Integration §6 / F-52).** When a run that came from Job Radar
*completes*, cv-tailor POSTs summary metrics back to
`JOB_RADAR_API_URL/api/cv-tailor-results` with `Authorization: Bearer <JOB_RADAR_SERVICE_KEY>` and
`source: "cv_tailor_api"`. Three metrics, names/scales matching Job Radar's schema:
`fit_score` (0–1, Phase-1 `overall_fit_score`), `coverage_score` (0–1, final grounded
`keyword_coverage`, F-38), `cv_quality_score` (0–10, the latest iteration's aggregate
`critique_score` — the same "CV quality" the Scores tab + report header show), plus `cvcm_enabled`,
`tailoring_mode`, and an `output_link`. Metrics are read from the run's on-disk checkpoints
(decoupled from `run_pipeline`'s return), degrading to `null` rather than blocking.

- **Opt-in by config:** the callback fires only when `JOB_RADAR_SERVICE_KEY` is set; unset ⇒ skipped
  silently (exactly Phase-2 behaviour). `CV_TAILOR_BASE_URL` sets the `output_link` host.
- **Best-effort, never in the critical path:** synchronous `httpx.post` (the run completes on a
  worker thread — no event loop to schedule onto), 5 s timeout, **never raises**; a failure logs a
  warning and the run still completes. `run_complete` fires *before* the callback, so the browser is
  never blocked on it.
- **UI indicator:** a single `job_radar_linked` SSE event ({ok}) is emitted after the callback, shown
  in the run summary as *✓ Linked back to Job Radar* / *⚠ Could not link back to Job Radar — add
  metrics manually*. Because the browser closes its stream on `run_complete`, the Run page keeps the
  EventSource open briefly (grace window) for a Job Radar run to catch this trailing event.
