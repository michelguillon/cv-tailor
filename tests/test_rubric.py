"""Rubric update logic tests (D-04) with a mocked Claude client (no API)."""

import types

from tailor.models import JDAnalysis, RubricAddition, ScoringRubric
from tailor.tools.rubric import validate_rubric_additions


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA", "kubernetes"], ["fintech"], "payments", ["technical"])


def rubric(required=("pre-sales", "emea"), added=()):
    return ScoringRubric(1, list(required), ["fintech"], [], "t", "t", list(added))


def fake_claude(*decision_sets, no_tool=False):
    """Returns a submit_rubric_decisions tool_use per call (or no tool_use if no_tool)."""
    calls = {"n": 0}

    def create(**kwargs):
        i = min(calls["n"], len(decision_sets) - 1) if decision_sets else 0
        calls["n"] += 1
        if no_tool:
            block = types.SimpleNamespace(type="text", text="no tool call")
        else:
            block = types.SimpleNamespace(type="tool_use", name="submit_rubric_decisions",
                                          input={"decisions": list(decision_sets[i])})
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=10))

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def boom_client():
    def create(**kwargs):
        raise AssertionError("Claude must NOT be called when there are no new candidates")
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def dec(keyword, implied, reason="r"):
    return {"keyword": keyword, "implied_by_jd": implied, "reason": reason}


def test_accepts_implied_addition_bumps_version_with_provenance():
    r = rubric()
    out, added = validate_rubric_additions(
        r, ["kubernetes"], jd(), 2, model="m",
        client=fake_claude([dec("kubernetes", True, "JD lists it")]),
    )
    assert out.version == 2                      # incremented on real change
    assert "kubernetes" in out.required_keywords
    assert len(added) == 1 and isinstance(added[0], RubricAddition)
    assert added[0].added_in_iteration == 2 and added[0].keyword == "kubernetes"
    assert out.added_from_critique == added


def test_rejected_addition_leaves_rubric_unchanged():
    r = rubric()
    out, added = validate_rubric_additions(
        r, ["astrology"], jd(), 1, model="m",
        client=fake_claude([dec("astrology", False, "not in JD")]),
    )
    assert out is r and out.version == 1 and added == []


def test_caps_additions_per_iteration():
    r = rubric()
    out, added = validate_rubric_additions(
        r, ["k8s", "terraform", "grpc"], jd(), 1, model="m", max_additions=2,
        client=fake_claude([dec("k8s", True), dec("terraform", True), dec("grpc", True)]),
    )
    assert len(added) == 2                        # third accepted-but-capped
    assert out.version == 2


def test_dedups_existing_keyword_without_calling_model():
    r = rubric(required=("pre-sales", "emea"))
    # "EMEA" already in the rubric (case/spacing-insensitive) → no candidates → no API call
    out, added = validate_rubric_additions(r, ["EMEA"], jd(), 1, model="m", client=boom_client())
    assert out is r and added == []


def test_model_failure_keeps_rubric_unchanged():
    r = rubric()
    out, added = validate_rubric_additions(
        r, ["kubernetes"], jd(), 1, model="m", client=fake_claude(no_tool=True),
    )
    assert out is r and out.version == 1 and added == []
