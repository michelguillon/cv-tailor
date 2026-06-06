"""Orchestrator tool tests (D-28), mocked Anthropic — no API."""

import types

import pytest

from tailor.models import JDAnalysis, ScoringRubric, WriterDraft
from tailor.tools import orchestrator_tool
from tailor.tools.orchestrator_tool import OrchestratorError, adjudicate, read_pushbacks


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], ["fintech"], "payments", ["technical"])


def rubric():
    return ScoringRubric(1, ["alpha", "beta"], [], [], "t", "t", [])


def draft(writer, text):
    return WriterDraft(writer, "profile", text, version=1)


def fake_claude(*decisions, directions=None):
    """submit_decision → next decision dict; revise_direction → next direction."""
    q = {"dec": list(decisions), "dir": list(directions or [])}

    def create(**kwargs):
        name = kwargs["tool_choice"]["name"]
        if name == "submit_decision":
            inp = q["dec"].pop(0) if q["dec"] else {}
        else:  # revise_direction
            d = q["dir"].pop(0) if q["dir"] else "hold"
            inp = {"direction": d, "revised": True, "reasoning": "r"}
        block = types.SimpleNamespace(type="tool_use", name=name, input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=10))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def decision(base="claude", cq=8.0, gq=6.0, converged=False, final_text="", additions=None):
    return {"selected_base": base, "final_text": final_text, "direction": "tighten the opening",
            "synthesis_notes": None, "claude_quality": cq, "gpt_quality": gq,
            "converged": converged, "rubric_additions": additions or []}


def test_selects_claude_uses_claude_text_verbatim():
    dec, text = adjudicate("profile", draft("claude", "alpha beta claude"), draft("gpt", "alpha gpt"),
                           rubric(), jd(), model="m", client=fake_claude(decision(base="claude")))
    assert dec.selected_base == "claude" and text == "alpha beta claude"
    assert dec.keyword_coverage == 1.0          # computed in code on the selected text
    assert dec.claude_quality == 8.0 and dec.gpt_quality == 6.0


def test_synthesis_uses_orchestrator_final_text():
    dec, text = adjudicate("profile", draft("claude", "alpha"), draft("gpt", "beta"),
                           rubric(), jd(), model="m",
                           client=fake_claude(decision(base="synthesis", final_text="alpha beta merged")))
    assert dec.selected_base == "synthesis" and text == "alpha beta merged"
    assert dec.keyword_coverage == 1.0


def test_retries_then_raises_on_bad_selected_base():
    bad = decision(base="neither")
    with pytest.raises(OrchestratorError):
        adjudicate("profile", draft("claude", "a"), draft("gpt", "b"), rubric(), jd(),
                   model="m", client=fake_claude(bad, bad))


def test_synthesis_without_final_text_is_invalid():
    bad = decision(base="synthesis", final_text="")
    with pytest.raises(OrchestratorError):
        adjudicate("profile", draft("claude", "a"), draft("gpt", "b"), rubric(), jd(),
                   model="m", client=fake_claude(bad, bad))


def test_source_text_is_passed_to_the_orchestrator_for_grounding():
    """Fix C / F-34: the orchestrator must see the SOURCE the writers drafted from, so it
    can check each draft against it and gate fabrication."""
    captured = {}

    def create(**kwargs):
        captured["user"] = kwargs["messages"][0]["content"]
        block = types.SimpleNamespace(type="tool_use", name="submit_decision",
                                      input=decision(base="claude"))
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    adjudicate("profile", draft("claude", "a"), draft("gpt", "b"), rubric(), jd(),
               source_text="ADTECH IDENTITY WORK — NO FINTECH", model="m", client=client)
    assert "ADTECH IDENTITY WORK — NO FINTECH" in captured["user"]
    assert "ground truth" in captured["user"].lower()


def test_cvcm_reaches_the_prompt_only_when_present():
    """The value model is threaded into the orchestrator's user message for the tiebreak,
    and omitted entirely when absent (no empty CVCM block) — §3.9/D-33."""
    seen = {}

    def make_client():
        def create(**kwargs):
            seen["user"] = kwargs["messages"][0]["content"]
            block = types.SimpleNamespace(type="tool_use", name="submit_decision",
                                          input=decision(base="claude"))
            return types.SimpleNamespace(content=[block],
                                         usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))
        return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    adjudicate("profile", draft("claude", "a"), draft("gpt", "b"), rubric(), jd(),
               cvcm="I turn ambiguity into commercial outcomes.", model="m", client=make_client())
    assert "CANDIDATE VALUE MODEL" in seen["user"] and "commercial outcomes" in seen["user"]

    adjudicate("profile", draft("claude", "a"), draft("gpt", "b"), rubric(), jd(),
               model="m", client=make_client())
    assert "CANDIDATE VALUE MODEL" not in seen["user"]


def test_read_pushbacks_no_objection_returns_direction_without_calling():
    dec = types.SimpleNamespace(direction="keep going")

    def boom(**kwargs):
        raise AssertionError("must not call the model when neither writer pushed back")
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=boom))
    assert read_pushbacks("profile", dec, None, None, jd(), model="m", client=client) == "keep going"


def test_read_pushbacks_revises_on_objection():
    dec = types.SimpleNamespace(direction="cut metrics")
    out = read_pushbacks("profile", dec, "that removes the only quantified win", None, jd(),
                         model="m", client=fake_claude(directions=["keep the metric, trim prose"]))
    assert out == "keep the metric, trim prose"
