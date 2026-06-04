"""Phase 1 fit-assessment tests: deterministic composition + mocked Claude."""

import types

import pytest

from tailor.models import FitAssessment, JDAnalysis, ScoringRubric
from tailor.phases.phase1_fit_assessment import (
    FitAssessmentError,
    _company_key,
    assess_fit,
    build_composition,
    render_fit_hitl,
)

STATIC = ["header", "education", "languages", "certifications", "interests"]


def rubric(required):
    return ScoringRubric(1, required, [], [], "t", "t", [])


def sec(filename, section_id, section_type, document, *, static=False, company=""):
    return {
        "filename": filename, "section_id": section_id, "section_type": section_type,
        "document": document, "static": static, "company": company, "version_date": "2026-01-01",
    }


def corpus():
    # Two CVs. CV_A has the stronger profile; CV_B the stronger Microsoft experience.
    r = ["pre-sales", "emea", "payments", "api", "leadership"]  # noqa: F841 (doc only)
    return [
        sec("CV_A.docx", "profile", "profile", "pre-sales leader across EMEA payments and api platforms"),
        sec("CV_B.docx", "profile", "profile", "general manager"),
        sec("CV_A.docx", "skills", "skills", "leadership"),
        sec("CV_B.docx", "skills", "skills", "pre-sales api payments"),
        sec("CV_A.docx", "experience_microsoft_pm", "experience", "shipped product", company="Microsoft"),
        sec("CV_B.docx", "experience_microsoft_lead", "experience", "led pre-sales api payments in emea", company="Microsoft"),
        sec("CV_A.docx", "header", "header", "Jane Doe", static=True),
        sec("CV_B.docx", "header", "header", "Jane Doe", static=True),
        sec("CV_A.docx", "interests", "interests", "cycling", static=True),
        sec("CV_B.docx", "interests", "interests", "cycling", static=True),
    ]


# --------------------------------------------------------------------------- #
# Composition (deterministic)                                                 #
# --------------------------------------------------------------------------- #

def test_company_key_groups_variants():
    assert _company_key("Imagination Technologies, PowerVR Graphics") == _company_key("Imagination Technologies")


def test_section_mix_picks_best_variant_per_section():
    r = rubric(["pre-sales", "emea", "payments", "api", "leadership"])
    recommended, diag = build_composition(corpus(), r, STATIC)
    assert recommended["profile"].source_cv == "CV_A"     # CV_A profile covers more
    assert recommended["skills"].source_cv == "CV_B"      # CV_B skills covers more
    # experience mixed per company → Microsoft taken from CV_B (covers 4 keywords)
    assert recommended["experience_microsoft_lead"].source_cv == "CV_B"
    assert "experience_microsoft_pm" not in recommended


def test_static_sections_from_primary_base():
    r = rubric(["pre-sales", "emea", "payments", "api", "leadership"])
    recommended, diag = build_composition(corpus(), r, STATIC)
    # primary base = best overall non-static coverage; header/interests come from it
    assert recommended["header"].source_cv == diag["primary_base"]
    assert recommended["interests"].reason.startswith("static")


def test_diagnostics_have_matched_and_missing():
    r = rubric(["pre-sales", "emea", "payments", "api", "leadership", "kubernetes"])
    _, diag = build_composition(corpus(), r, STATIC)
    assert "kubernetes" in diag["missing_keywords"]
    assert 0.0 <= diag["composed_coverage"] <= 1.0


# --------------------------------------------------------------------------- #
# assess_fit (mocked Claude)                                                  #
# --------------------------------------------------------------------------- #

def jd():
    return JDAnalysis(
        raw_text="...", role_title="Director, Solutions Engineering", seniority_level="director",
        key_requirements=["lead EMEA SE"], nice_to_haves=["fintech"],
        company_context="payments platform", tone_signals=["technical"],
    )


def fake_anthropic(*tool_inputs):
    """Client whose messages.create returns a tool_use block per call."""
    calls = {"n": 0}

    def create(**kwargs):
        inp = tool_inputs[min(calls["n"], len(tool_inputs) - 1)]
        calls["n"] += 1
        if inp is None:    # simulate "model didn't call the tool"
            block = types.SimpleNamespace(type="text", text="hmm")
        else:
            block = types.SimpleNamespace(type="tool_use", name="submit_fit_assessment", input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


PARTIAL = {
    "outcome": "partial",
    "skills_transferable": ["pre-sales leadership", "EMEA delivery"],
    "gaps": [{"requirement": "payments domain", "gap_type": "experience",
              "addressable": True, "severity": "major", "reason": "limited fintech exposure"}],
    "no_fit_reason": None,
}


def test_assess_fit_partial():
    r = rubric(["pre-sales", "emea", "payments"])
    fit, usage = assess_fit(jd(), r, model="m", config={"static_sections": STATIC},
                            client=fake_anthropic(PARTIAL), sections=corpus())
    assert isinstance(fit, FitAssessment)
    assert fit.outcome == "partial"
    assert fit.recommended_sections is not None
    assert fit.gaps[0].gap_type == "experience" and fit.gaps[0].addressable
    assert 0.0 <= fit.overall_fit_score <= 1.0


def test_assess_fit_no_fit_drops_recommendations():
    no_fit = {"outcome": "no_fit", "skills_transferable": [],
              "gaps": [{"requirement": "SC clearance", "gap_type": "hard_requirement",
                        "addressable": False, "severity": "blocking", "reason": "not held"}],
              "no_fit_reason": "Requires active SC clearance, which no CV holds."}
    fit, _ = assess_fit(jd(), rubric(["pre-sales"]), model="m", config={"static_sections": STATIC},
                        client=fake_anthropic(no_fit), sections=corpus())
    assert fit.outcome == "no_fit"
    assert fit.recommended_sections is None
    assert "clearance" in fit.no_fit_reason


def test_assess_fit_retries_then_succeeds():
    bad = {**PARTIAL, "gaps": [{**PARTIAL["gaps"][0], "gap_type": "invented"}]}  # bad gap_type
    fit, _ = assess_fit(jd(), rubric(["pre-sales"]), model="m", config={"static_sections": STATIC},
                        client=fake_anthropic(bad, PARTIAL), sections=corpus())
    assert fit.outcome == "partial"


def test_assess_fit_raises_on_persistent_invalid():
    bad = {"outcome": "maybe", "skills_transferable": [], "gaps": []}  # bad outcome
    with pytest.raises(FitAssessmentError):
        assess_fit(jd(), rubric(["pre-sales"]), model="m", config={"static_sections": STATIC},
                   client=fake_anthropic(bad, bad), sections=corpus())


def test_assess_fit_raises_when_tool_not_called():
    with pytest.raises(FitAssessmentError):
        assess_fit(jd(), rubric(["pre-sales"]), model="m", config={"static_sections": STATIC},
                   client=fake_anthropic(None, None), sections=corpus())


def test_render_hitl_readable():
    fit, _ = assess_fit(jd(), rubric(["pre-sales", "emea"]), model="m",
                        config={"static_sections": STATIC}, client=fake_anthropic(PARTIAL), sections=corpus())
    out = render_fit_hitl(fit, jd())
    assert "Fit Assessment" in out and "PARTIAL" in out
    assert "section mix" in out.lower() and "[p]roceed" in out
