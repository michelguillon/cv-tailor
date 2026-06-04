"""Deterministic tests for tools/scorer.py (no API)."""

from tailor.models import ScoringRubric
from tailor.tools.scorer import (
    coverage_report,
    keyword_coverage,
    keyword_present,
    matched_keywords,
    normalise,
    union_coverage,
)


def rubric(required, nice=None):
    return ScoringRubric(
        version=1,
        required_keywords=required,
        nice_to_have_keywords=nice or [],
        structural_requirements=[],
        created_at="t", updated_at="t",
    )


def test_normalise_collapses_punctuation_and_case():
    assert normalise("Pre-Sales,  GTM!") == "pre sales gtm"


def test_keyword_present_token_aligned():
    norm = normalise("Led an API-driven platform for payments")
    assert keyword_present("api", norm)          # hyphen split → token "api"
    assert keyword_present("api driven", norm)
    assert keyword_present("payments", norm)
    assert not keyword_present("pay", norm)       # not a whole token
    assert not keyword_present("apis", norm)      # no stemming (documented v1 limit)


def test_hyphen_space_equivalence():
    norm = normalise("strong pre-sales and go to market skills")
    assert keyword_present("pre-sales", norm)     # keyword hyphenated, text hyphenated
    assert keyword_present("go-to-market", norm)  # keyword hyphenated, text spaced


def test_token_subset_matches_reworded_phrase():
    """F-10: a multi-word keyword matches when its significant tokens are present
    but non-adjacent / reworded (the real-data brittleness fix)."""
    norm = normalise("We shaped the go-to-market plans and the overall strategy.")
    assert keyword_present("go-to-market strategy", norm)   # go, market, strategy all present
    norm2 = normalise("Owned executive-level stakeholder communication.")
    assert keyword_present("executive communication", norm2)


def test_token_subset_requires_all_significant_tokens():
    norm = normalise("Ran enterprise sales across the region.")
    assert not keyword_present("enterprise sales cycles", norm)  # 'cycles' missing
    assert keyword_present("enterprise sales", norm)


def test_single_token_is_contiguous_only():
    """A single-token keyword must appear as a whole token, never token-subset."""
    norm = normalise("rapidly scaled apis")
    assert not keyword_present("api", norm)     # 'apis' != 'api', no stemming


def test_keyword_coverage_fraction():
    r = rubric(["pre-sales", "emea", "kubernetes", "payments"])
    text = "Led pre-sales across EMEA for a payments platform."
    # 3 of 4 present (kubernetes missing)
    assert keyword_coverage(text, r) == 0.75


def test_keyword_coverage_empty_rubric_is_zero():
    assert keyword_coverage("anything", rubric([])) == 0.0


def test_matched_keywords_preserves_order():
    r = ["emea", "pre-sales", "payments"]
    assert matched_keywords("pre-sales in EMEA", r) == ["emea", "pre-sales"]


def test_union_coverage_across_sections():
    r = rubric(["pre-sales", "emea", "kubernetes", "payments"])
    sections = [
        "Profile: pre-sales leader",        # pre-sales
        "Experience: scaled EMEA payments",  # emea, payments
    ]
    # union = 3/4 (kubernetes still missing); higher than any single section
    assert union_coverage(sections, r) == 0.75
    assert keyword_coverage(sections[0], r) == 0.25


def test_coverage_report_matched_and_missing():
    r = rubric(["pre-sales", "emea", "kubernetes"], nice=["terraform", "aws"])
    rep = coverage_report("Pre-sales across EMEA, built on AWS.", r)
    assert rep.matched == ["pre-sales", "emea"]
    assert rep.missing == ["kubernetes"]
    assert rep.coverage == 2 / 3
    assert rep.nice_to_have_matched == ["aws"]
    assert rep.nice_to_have_coverage == 0.5


def test_coverage_is_deterministic():
    """Same input → same score (the convergence signal depends on this)."""
    r = rubric(["pre-sales", "emea", "payments"])
    text = "pre-sales in EMEA"
    assert keyword_coverage(text, r) == keyword_coverage(text, r)
