"""All inter-stage schemas for cv-tailor (SPEC_ORCHESTRATOR.md §4).

Every dataclass is JSON-serialisable and checkpointed to outputs/<run_id>/ at
the end of its producing stage (the Week 2 checkpoint pattern). Because these
schemas are communication contracts *between providers*, not just between
modules, the blast radius of a post-build change is large (LEARNING_NOTES D-07);
Step 0 builds them carefully and tests round-trips before any phase exists.

Serialisation is provided by the `Serializable` mixin: `to_dict`/`to_json` use
`dataclasses.asdict` (recursive), and `from_dict`/`from_json` use a generic,
type-hint-driven recursive deserialiser that reconstructs nested dataclasses,
`list[X]`, `dict[str, X]`, and `X | None` without per-class boilerplate.

Class definition order is dependency-first so runtime type annotations
(`list[RubricAddition]`, `dict[str, SectionScore]`, ...) resolve eagerly.
"""

from __future__ import annotations

import json
import types
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass

__all__ = [
    "Serializable",
    "JDAnalysis",
    "RubricAddition",
    "ScoringRubric",
    "CVSection",
    "CVMetadata",
    "SectionBudget",
    "CVMatch",
    "SectionRecommendation",
    "FitGap",
    "FitAssessment",
    "SectionScore",
    "IterationScore",
    "CritiqueItem",
    "Critique",
    "ReasoningEntry",
    "PipelineOutput",
]


# --------------------------------------------------------------------------- #
# Generic (de)serialisation                                                   #
# --------------------------------------------------------------------------- #

_NONE_TYPE = type(None)
# typing.Union for Optional[...]; types.UnionType for `X | None` (PEP 604).
_UNION_ORIGINS = (typing.Union, getattr(types, "UnionType", typing.Union))


def _convert(type_hint, value):
    """Recursively coerce a JSON-decoded ``value`` to match ``type_hint``."""
    if value is None:
        return None

    origin = typing.get_origin(type_hint)

    if origin in _UNION_ORIGINS:
        # Schemas only use `X | None`; coerce to the single non-None member.
        for arg in typing.get_args(type_hint):
            if arg is _NONE_TYPE:
                continue
            try:
                return _convert(arg, value)
            except Exception:
                continue
        return value

    if origin in (list, set, tuple):
        args = typing.get_args(type_hint) or (typing.Any,)
        elem_hint = args[0]
        return [_convert(elem_hint, item) for item in value]

    if origin is dict:
        args = typing.get_args(type_hint)
        val_hint = args[1] if len(args) == 2 else typing.Any
        return {key: _convert(val_hint, val) for key, val in value.items()}

    if is_dataclass(type_hint) and isinstance(type_hint, type):
        return _from_dict(type_hint, value)

    return value


def _from_dict(cls, data):
    """Reconstruct a dataclass instance from a plain dict.

    Unknown keys are ignored (forward-compatible reads); missing required
    fields raise ``TypeError`` via the constructor, which is the behaviour the
    "required fields" tests assert.
    """
    if not isinstance(data, dict):
        raise TypeError(f"{cls.__name__}.from_dict expected a dict, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    field_names = {f.name for f in fields(cls)}
    kwargs = {
        key: _convert(hints.get(key, typing.Any), val)
        for key, val in data.items()
        if key in field_names
    }
    return cls(**kwargs)


class Serializable:
    """Mixin giving every schema dataclass round-trippable JSON helpers."""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict):
        return _from_dict(cls, data)

    @classmethod
    def from_json(cls, raw: str):
        return cls.from_dict(json.loads(raw))


# --------------------------------------------------------------------------- #
# Phase 0 — JD analysis + scoring rubric                                      #
# --------------------------------------------------------------------------- #

@dataclass
class JDAnalysis(Serializable):
    raw_text: str
    role_title: str
    seniority_level: str          # inferred
    key_requirements: list[str]
    nice_to_haves: list[str]
    company_context: str
    tone_signals: list[str]       # e.g. "technical", "startup", "formal"


@dataclass
class RubricAddition(Serializable):
    keyword: str
    added_in_iteration: int
    triggered_by: str             # description of the critique item that surfaced this


@dataclass
class ScoringRubric(Serializable):
    version: int                          # increments on each update
    required_keywords: list[str]          # from JD extraction
    nice_to_have_keywords: list[str]
    structural_requirements: list[str]    # e.g. "quantify achievements"
    created_at: str
    updated_at: str
    # Additions during the refinement loop, with provenance (D-07 correction #2).
    # Trails the required fields so v1 rubrics can start empty.
    added_from_critique: list[RubricAddition] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Corpus metadata (section-granular ingestion)                                #
# --------------------------------------------------------------------------- #

@dataclass
class CVSection(Serializable):
    section_id: str               # stable unique id, e.g. "experience_acme_corp_principal_2022"
    section_type: str             # canonical type from config.yaml cv_sections list
    position: int                 # order within this CV (0-indexed); governs assembly order
    static: bool = False          # if True: copied verbatim, excluded from critique/scoring
    word_count: int = 0           # measured from source docx at ingestion
    line_count: int = 0           # approximate rendered lines


@dataclass
class CVMetadata(Serializable):
    filename: str
    cv_type: str                  # "generic" | "job_specific"
    target_role: str              # e.g. "Solution Architect"
    target_company: str | None
    skills_emphasis: list[str]
    seniority: str                # "senior" | "principal" | "director"
    version_date: str
    sections: list[CVSection] = field(default_factory=list)  # ordered, present-only


@dataclass
class SectionBudget(Serializable):
    section_type: str
    min_words: int                # smallest this section appears across corpus
    max_words: int                # largest
    target_words: int             # median — working budget for drafting and critique


# --------------------------------------------------------------------------- #
# Phase 1 — fit assessment                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class CVMatch(Serializable):
    """Internal retrieval utility — not the output of Phase 1."""
    filename: str
    metadata: CVMetadata
    semantic_score: float
    keyword_coverage: float       # against initial rubric


@dataclass
class SectionRecommendation(Serializable):
    section_id: str
    source_cv: str                # filename of the CV this section is drawn from
    section_version: str          # which version file within that CV
    keyword_coverage: float       # this section's coverage against initial rubric
    reason: str                   # one-line: "best Skills coverage for ML role"


@dataclass
class FitGap(Serializable):
    requirement: str
    gap_type: str                 # "keyword" | "experience" | "hard_requirement" | "seniority"
    addressable: bool             # True = CV tailoring can close this; False = cannot
    severity: str                 # "minor" | "major" | "blocking"
    reason: str                   # one-line explanation


@dataclass
class FitAssessment(Serializable):
    outcome: str                  # "strong" | "partial" | "no_fit"
    overall_fit_score: float      # 0–1; weighted mean across section coverages
    skills_transferable: list[str] = field(default_factory=list)
    gaps: list[FitGap] = field(default_factory=list)
    # section_id → best source section across all CVs; None when outcome == "no_fit"
    recommended_sections: dict[str, SectionRecommendation] | None = None
    no_fit_reason: str | None = None  # plain-English explanation when outcome == "no_fit"


# --------------------------------------------------------------------------- #
# Phase 3 — refinement loop scoring                                           #
# --------------------------------------------------------------------------- #

@dataclass
class SectionScore(Serializable):
    section_id: str
    section_type: str
    keyword_coverage: float       # 0–1; proportion of rubric items present in this section
    critique_score: float | None = None  # 0–10; None if section frozen (static or converged)
    converged: bool = False       # True = frozen for remaining iterations
    current_version: int = 0      # which draft version of this section is active


@dataclass
class IterationScore(Serializable):
    iteration: int
    keyword_coverage: float       # weighted mean across non-static sections
    critique_score: float | None  # mean of non-frozen section scores
    keyword_delta: float          # vs previous iteration aggregate
    critique_delta: float
    sections_converged: int       # count of newly frozen sections this iteration
    sections_active: int          # count still being critiqued
    section_scores: dict[str, SectionScore] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Phase 3 — critique (GPT-4o-mini tool output)                                #
# --------------------------------------------------------------------------- #

@dataclass
class CritiqueItem(Serializable):
    section: str
    severity: str                 # "major" | "minor" (defined explicitly in the GPT prompt;
                                  #  the soft-stop condition depends on zero major items)
    issue: str
    suggestion: str
    accepted_by_orchestrator: bool = False
    rejection_reason: str | None = None   # if not accepted
    applied: bool = False         # True if acceptance was reflected in next draft
                                  # (accepted=True, applied=False is logged as an anomaly — D-07 #1)


@dataclass
class Critique(Serializable):
    overall_score: float          # 0–10
    section_scores: dict[str, float] = field(default_factory=dict)
    items: list[CritiqueItem] = field(default_factory=list)
    rubric_additions: list[str] = field(default_factory=list)  # new requirements surfaced


# --------------------------------------------------------------------------- #
# Audit trail                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class ReasoningEntry(Serializable):
    ts: str
    phase: str
    event: str
    reasoning: str
    iteration: int | None = None
    keyword_score: float | None = None
    critique_score: float | None = None
    rubric_version: int | None = None     # rubric version active when scores were computed (D-07 #4)


# --------------------------------------------------------------------------- #
# Pipeline summary (NOT a data warehouse — drafts live on disk, D-07 #3)      #
# --------------------------------------------------------------------------- #

@dataclass
class PipelineOutput(Serializable):
    run_id: str
    mode: str
    base_cv_filename: str
    jd_analysis: JDAnalysis
    fit_assessment: FitAssessment
    final_rubric: ScoringRubric
    final_cv_md: str
    converged: bool
    convergence_reason: str
    iterations: list[IterationScore] = field(default_factory=list)
    # per model: "anthropic_sonnet", "anthropic_haiku", "openai_gpt4o_mini", "mistral_small"
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    # Note: intermediate drafts are NOT stored here — each phase checkpoints its
    # section files to outputs/<run_id>/sections/; Phase 6 reads them from disk.
