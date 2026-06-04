"""Step 0 verification: schema serialisation round-trips and required fields.

Runs with NO API calls. `pytest tests/test_schemas.py` is the Step 0 gate.
"""

import dataclasses

import pytest

from tailor.models import (
    CritiqueItem,
    CVMatch,
    CVMetadata,
    CVSection,
    FitAssessment,
    FitGap,
    IterationScore,
    JDAnalysis,
    OrchestratorDecision,
    PipelineOutput,
    ReasoningEntry,
    RubricAddition,
    ScoringRubric,
    SectionBudget,
    SectionRecommendation,
    SectionScore,
    WriterDraft,
)


# --------------------------------------------------------------------------- #
# Fixtures — one representative instance per schema                           #
# --------------------------------------------------------------------------- #

def make_jd_analysis():
    return JDAnalysis(
        raw_text="We are hiring a Solution Architect...",
        role_title="Solution Architect",
        seniority_level="principal",
        key_requirements=["cloud architecture", "stakeholder management"],
        nice_to_haves=["Kubernetes"],
        company_context="Series C fintech scaling its platform team",
        tone_signals=["technical", "startup"],
    )


def make_rubric():
    return ScoringRubric(
        version=2,
        required_keywords=["cloud architecture", "AWS", "stakeholder management"],
        nice_to_have_keywords=["Kubernetes", "Terraform"],
        structural_requirements=["quantify achievements"],
        created_at="2026-06-03T14:00:00Z",
        updated_at="2026-06-03T14:30:00Z",
        added_from_critique=[
            RubricAddition(
                keyword="P&L ownership",
                added_in_iteration=2,
                triggered_by="critique flagged missing commercial accountability",
            )
        ],
    )


def make_cv_metadata():
    return CVMetadata(
        filename="solution_architect_ai_v2.docx",
        cv_type="job_specific",
        target_role="Solution Architect",
        target_company=None,
        skills_emphasis=["ML tooling", "cloud"],
        seniority="principal",
        version_date="2026-01-15",
        sections=[
            CVSection("profile", "profile", 0, static=False, word_count=85, line_count=6),
            CVSection("interests", "interests", 6, static=True, word_count=20, line_count=2),
        ],
    )


def make_fit_assessment():
    return FitAssessment(
        outcome="partial",
        overall_fit_score=0.74,
        skills_transferable=["cloud architecture", "delivery at scale"],
        gaps=[
            FitGap(
                requirement="Kubernetes experience",
                gap_type="experience",
                addressable=True,
                severity="major",
                reason="not prominent in any CV version",
            )
        ],
        recommended_sections={
            "profile": SectionRecommendation(
                section_id="profile",
                source_cv="solution_architect_generic_v3.docx",
                section_version="v3",
                keyword_coverage=0.81,
                reason="best Profile coverage for SA role",
            )
        },
        no_fit_reason=None,
    )


def make_iteration_score():
    return IterationScore(
        iteration=1,
        keyword_coverage=0.74,
        critique_score=7.8,
        keyword_delta=0.13,
        quality_delta=1.6,
        sections_converged=2,
        sections_active=3,
        section_scores={
            "profile": SectionScore(
                section_id="profile",
                section_type="profile",
                keyword_coverage=0.81,
                claude_quality=8.2,
                gpt_quality=7.5,
                selected_writer="claude",
                converged=True,
                current_version=2,
            )
        },
    )


def make_writer_draft():
    return WriterDraft(
        writer="gpt",
        section_id="experience_acme",
        text="Led delivery of ...",
        version=2,
        pushback="The direction drops the only quantified outcome.",
        items=[
            CritiqueItem(
                section="experience_acme",
                severity="major",
                issue="no quantified outcomes",
                suggestion="add team size and delivery metrics",
                source_writer="gpt",
            )
        ],
    )


def make_orchestrator_decision():
    return OrchestratorDecision(
        section_id="experience_acme",
        selected_base="synthesis",
        direction="keep Claude's framing, take GPT's metrics",
        keyword_coverage=0.79,
        claude_quality=7.5,
        gpt_quality=8.0,
        converged=False,
        synthesis_notes="Claude's opening line + GPT's quantified bullets",
        rubric_additions=["P&L ownership"],
    )


def make_pipeline_output():
    return PipelineOutput(
        run_id="run_20260603_001",
        mode="full",
        base_cv_filename="solution_architect_generic_v3.docx",
        jd_analysis=make_jd_analysis(),
        fit_assessment=make_fit_assessment(),
        final_rubric=make_rubric(),
        final_cv_md="# Jane Doe\n\nSolution Architect...",
        converged=True,
        convergence_reason="dual-signal: keyword_delta<0.05 and quality_delta<0.5",
        iterations=[make_iteration_score()],
        cost_breakdown={
            "anthropic_sonnet": 0.0441,
            "anthropic_haiku": 0.0012,
            "openai_gpt4o_mini": 0.0089,
            "mistral_small": 0.0018,
        },
    )


ALL_INSTANCES = [
    make_jd_analysis(),
    RubricAddition("AWS", 1, "JD mentions cloud platform"),
    make_rubric(),
    CVSection("experience_acme", "experience", 2, word_count=120, line_count=9),
    make_cv_metadata(),
    SectionBudget("experience", min_words=60, max_words=180, target_words=120),
    CVMatch("cv.docx", make_cv_metadata(), semantic_score=0.88, keyword_coverage=0.74),
    SectionRecommendation("skills", "cv.docx", "v2", 0.88, "best ML tooling"),
    FitGap("SC clearance", "hard_requirement", False, "blocking", "no CV mentions it"),
    make_fit_assessment(),
    SectionScore("profile", "profile", 0.81, claude_quality=8.2, gpt_quality=7.0,
                 selected_writer="claude", converged=True, current_version=2),
    make_iteration_score(),
    CritiqueItem("profile", "minor", "add cloud mention", "mention AWS", source_writer="claude"),
    make_writer_draft(),
    make_orchestrator_decision(),
    ReasoningEntry(
        ts="2026-06-03T14:23:01Z",
        phase="refinement_loop",
        event="critique_item_rejected",
        reasoning="JD explicitly requires metrics; rejected removal.",
        iteration=2,
        keyword_score=0.71,
        critique_score=7.4,
        rubric_version=2,
    ),
    make_pipeline_output(),
]


# --------------------------------------------------------------------------- #
# Round-trip tests                                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("instance", ALL_INSTANCES, ids=lambda i: type(i).__name__)
def test_dict_round_trip(instance):
    """to_dict → from_dict reproduces an equal object (nested types included)."""
    cls = type(instance)
    restored = cls.from_dict(instance.to_dict())
    assert restored == instance


@pytest.mark.parametrize("instance", ALL_INSTANCES, ids=lambda i: type(i).__name__)
def test_json_round_trip(instance):
    """to_json → from_json reproduces an equal object."""
    cls = type(instance)
    restored = cls.from_json(instance.to_json())
    assert restored == instance


def test_nested_reconstruction_types():
    """Nested fields deserialise to the correct dataclass types, not dicts."""
    po = PipelineOutput.from_dict(make_pipeline_output().to_dict())
    assert isinstance(po.jd_analysis, JDAnalysis)
    assert isinstance(po.fit_assessment, FitAssessment)
    assert isinstance(po.final_rubric, ScoringRubric)
    assert isinstance(po.final_rubric.added_from_critique[0], RubricAddition)
    assert isinstance(po.fit_assessment.gaps[0], FitGap)
    rec = po.fit_assessment.recommended_sections["profile"]
    assert isinstance(rec, SectionRecommendation)
    assert isinstance(po.iterations[0], IterationScore)
    assert isinstance(po.iterations[0].section_scores["profile"], SectionScore)


def test_optional_none_round_trips():
    """`X | None` fields preserve None through the round-trip."""
    fa = FitAssessment(outcome="no_fit", overall_fit_score=0.2, no_fit_reason="needs SC clearance")
    assert fa.recommended_sections is None
    restored = FitAssessment.from_dict(fa.to_dict())
    assert restored.recommended_sections is None
    assert restored.no_fit_reason == "needs SC clearance"


def test_to_dict_is_plain_json_types():
    """to_dict produces only JSON-native types (dict/list/str/int/float/bool/None)."""
    import json

    # Should not raise; output is plain types all the way down.
    json.dumps(make_pipeline_output().to_dict())


def test_extra_keys_ignored():
    """Forward-compatible reads: unknown keys are dropped, not errors."""
    data = make_jd_analysis().to_dict()
    data["some_future_field"] = "ignored"
    restored = JDAnalysis.from_dict(data)
    assert restored == make_jd_analysis()


# --------------------------------------------------------------------------- #
# Required-field tests                                                        #
# --------------------------------------------------------------------------- #

def test_missing_required_field_raises():
    data = make_jd_analysis().to_dict()
    del data["role_title"]
    with pytest.raises(TypeError):
        JDAnalysis.from_dict(data)


def test_from_dict_rejects_non_dict():
    with pytest.raises(TypeError):
        JDAnalysis.from_dict(["not", "a", "dict"])


# --------------------------------------------------------------------------- #
# D-07 / D-11 schema-correction guards                                        #
# --------------------------------------------------------------------------- #

def _field_names(cls):
    return {f.name for f in dataclasses.fields(cls)}


def test_d28_critique_item_is_writer_sourced():
    """Dual-writer CritiqueItem: writers self-assess (source_writer); the old
    orchestrator accept/apply fields are gone (no separate critique tool, D-28)."""
    names = _field_names(CritiqueItem)
    assert "source_writer" in names
    assert {"accepted_by_orchestrator", "rejection_reason", "applied"}.isdisjoint(names)


def test_d28_writer_draft_carries_items():
    """WriterDraft holds the writer's self-flagged items — the canonical source
    for the zero-major soft-stop / freeze check (D-28)."""
    names = _field_names(WriterDraft)
    assert {"writer", "text", "version", "pushback", "items"} <= names
    wd = WriterDraft.from_dict(make_writer_draft().to_dict())
    assert isinstance(wd.items[0], CritiqueItem)


def test_d07_rubric_additions_are_typed():
    """added_from_critique is list[RubricAddition], not list[str]."""
    r = make_rubric()
    assert all(isinstance(a, RubricAddition) for a in r.added_from_critique)
    restored = ScoringRubric.from_dict(r.to_dict())
    assert isinstance(restored.added_from_critique[0], RubricAddition)


def test_d07_pipeline_output_does_not_store_drafts():
    names = _field_names(PipelineOutput)
    assert "drafts" not in names
    assert "final_cv_md" in names  # summary keeps only the final assembled CV


def test_d07_reasoning_entry_has_rubric_version():
    assert "rubric_version" in _field_names(ReasoningEntry)


def test_d11_critique_severity_is_two_level():
    """Severity definitions live in the prompt; the schema carries the string.
    Guard that both labels round-trip cleanly."""
    for sev in ("major", "minor"):
        item = CritiqueItem("profile", sev, "issue", "suggestion", source_writer="claude")
        assert CritiqueItem.from_dict(item.to_dict()).severity == sev
