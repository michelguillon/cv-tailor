"""Critique tool tests with a mocked OpenAI client (no API)."""

import json
import types

import pytest

from tailor.models import Critique, JDAnalysis, ScoringRubric, SectionBudget
from tailor.tools.critique import CritiqueError, critique_sections


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], ["fintech"], "payments", ["technical"])


def rubric():
    return ScoringRubric(1, ["pre-sales", "emea"], ["fintech"], [], "t", "t", [])


def fake_openai(*payloads):
    """Client whose chat.completions.create returns canned JSON content per call."""
    calls = {"n": 0}

    def create(**kwargs):
        payload = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        content = payload if isinstance(payload, str) else json.dumps(payload)
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=80))

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


VALID = {
    "overall_score": 7.0,
    "section_scores": [{"section": "profile", "score": 7.5}, {"section": "skills", "score": 6.0}],
    "items": [
        {"section": "profile", "severity": "major", "issue": "no EMEA leadership evidence", "suggestion": "add it"},
        {"section": "skills", "severity": "minor", "issue": "could list tools", "suggestion": "add tools"},
    ],
    "rubric_additions": ["api platform"],
}


def test_valid_critique_parsed():
    c = critique_sections({"profile": "p", "skills": "s"}, jd(), rubric(),
                          client=fake_openai(VALID))
    assert isinstance(c, Critique)
    assert c.overall_score == 7.0
    assert c.section_scores == {"profile": 7.5, "skills": 6.0}
    assert {i.severity for i in c.items} == {"major", "minor"}
    assert c.rubric_additions == ["api platform"]
    # orchestrator-owned fields default unset
    assert all(i.accepted_by_orchestrator is False and i.applied is False for i in c.items)


def test_empty_sections_returns_clean_score():
    c = critique_sections({}, jd(), rubric(), client=fake_openai(VALID))
    assert c.overall_score == 10.0 and c.items == []


VALID_PROFILE = {
    "overall_score": 7.0,
    "section_scores": [{"section": "profile", "score": 7.5}],
    "items": [{"section": "profile", "severity": "major", "issue": "no EMEA evidence", "suggestion": "add it"}],
    "rubric_additions": [],
}


def test_retries_then_succeeds_on_bad_score():
    bad = {**VALID_PROFILE, "overall_score": 42}    # out of 0–10
    c = critique_sections({"profile": "p"}, jd(), rubric(), client=fake_openai(bad, VALID_PROFILE))
    assert c.overall_score == 7.0


def test_raises_on_persistent_invalid_section_reference():
    bad = {**VALID, "items": [{"section": "ghost", "severity": "major", "issue": "x", "suggestion": "y"}]}
    with pytest.raises(CritiqueError):
        critique_sections({"profile": "p"}, jd(), rubric(), client=fake_openai(bad, bad))


def test_raises_on_unparseable():
    with pytest.raises(CritiqueError):
        critique_sections({"profile": "p"}, jd(), rubric(), client=fake_openai("not json", "still not"))


# --------------------------------------------------------------------------- #
# Deterministic length items (D-14) — computed in code, not by GPT            #
# --------------------------------------------------------------------------- #

def test_length_item_major_when_over_budget():
    budgets = {"profile": SectionBudget("profile", 70, 100, 85)}
    long_text = " ".join(["word"] * 150)            # 150 > 100 max
    clean = {**VALID, "items": [], "section_scores": [{"section": "profile", "score": 8}]}
    c = critique_sections({"profile": long_text}, jd(), rubric(),
                          client=fake_openai(clean), budgets=budgets, section_types={"profile": "profile"})
    length_items = [i for i in c.items if "budget" in i.issue]
    assert length_items and length_items[0].severity == "major"


def test_length_item_minor_when_under_budget():
    budgets = {"profile": SectionBudget("profile", 70, 100, 85)}
    short = "word word word"                          # 3 << 70*0.7
    clean = {**VALID, "items": [], "section_scores": [{"section": "profile", "score": 8}]}
    c = critique_sections({"profile": short}, jd(), rubric(),
                          client=fake_openai(clean), budgets=budgets, section_types={"profile": "profile"})
    assert any(i.severity == "minor" and "below" in i.issue for i in c.items)


def test_no_length_items_without_budgets():
    c = critique_sections({"profile": " ".join(["w"] * 500)}, jd(), rubric(), client=fake_openai(VALID_PROFILE))
    assert all("budget" not in i.issue for i in c.items)
