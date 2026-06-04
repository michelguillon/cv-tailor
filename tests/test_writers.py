"""Dual-writer tool tests (D-28), mocked providers — no API.

claude_writer (Anthropic tool_use) and gpt_writer (OpenAI strict json) share an
interface, so they share most assertions. Length-budget items are deterministic
(D-14): code counts words for BOTH writers (F-17).
"""

import json
import types

import pytest

from tailor.models import JDAnalysis, ScoringRubric, SectionBudget, WriterDraft
from tailor.tools import claude_writer, gpt_writer
from tailor.tools.claude_writer import WriterError


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], ["fintech"], "payments", ["technical"])


def rubric():
    return ScoringRubric(1, ["pre-sales", "emea"], ["fintech"], [], "t", "t", [])


def budget():
    return SectionBudget("profile", 5, 60, 30)


# -- mocks ------------------------------------------------------------------- #

def fake_claude(draft=None, items=None, pushback=None):
    """Anthropic mock: submit_draft → draft+items; submit_pushback → pushback."""
    def create(**kwargs):
        name = kwargs["tool_choice"]["name"]
        if name == "submit_draft":
            inp = {"text": draft if draft is not None else "Tailored pre-sales leader across EMEA.",
                   "items": items or []}
        else:  # submit_pushback
            dis = pushback is not None
            inp = {"disagree": dis, "reasoning": pushback or ""}
        block = types.SimpleNamespace(type="tool_use", name=name, input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=10))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def fake_gpt(draft=None, items=None, pushback=None, raw=None):
    """OpenAI mock: dispatch by response_format name (writer_draft vs pushback)."""
    def create(**kwargs):
        fmt = kwargs["response_format"]["json_schema"]["name"]
        if raw is not None:
            content = raw
        elif fmt == "writer_draft":
            content = json.dumps({"text": draft if draft is not None else "Bold pre-sales leader, EMEA.",
                                  "items": items or []})
        else:
            content = json.dumps({"disagree": pushback is not None, "reasoning": pushback or ""})
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=10))
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def item(sev="major", issue="weak", suggestion="fix"):
    return {"severity": sev, "issue": issue, "suggestion": suggestion}


# -- claude_writer ----------------------------------------------------------- #

def test_claude_writer_returns_validated_draft():
    items = [item("major", "no EMEA evidence", "add it")]
    d = claude_writer.write_section("profile", "old profile text here", jd(), rubric(), budget(),
                                    version=1, model="m", client=fake_claude("New EMEA profile.", items))
    assert isinstance(d, WriterDraft) and d.writer == "claude" and d.version == 1
    assert d.text == "New EMEA profile." and d.pushback is None
    assert d.items[0].source_writer == "claude" and d.items[0].severity == "major"


def test_claude_writer_appends_length_major_over_budget():
    long_text = " ".join(["word"] * 80)   # > 60 max
    d = claude_writer.write_section("profile", "x", jd(), rubric(), budget(),
                                    version=1, model="m", client=fake_claude(long_text, []))
    assert any(i.severity == "major" and "budget" in i.issue and i.source_writer == "claude" for i in d.items)


def test_claude_writer_retries_then_raises_on_empty_text():
    with pytest.raises(WriterError):
        claude_writer.write_section("profile", "x", jd(), rubric(), budget(),
                                    version=1, model="m", client=fake_claude("", []))


def test_claude_writer_pushback_string_when_disagrees_else_none():
    dec = types.SimpleNamespace(selected_base="gpt", claude_quality=6.0, direction="cut the intro")
    draft = WriterDraft("claude", "profile", "txt", 1)
    got = claude_writer.pushback("profile", dec, draft, jd(), model="m",
                                 client=fake_claude(pushback="that drops real evidence"))
    assert got == "that drops real evidence"
    agree = claude_writer.pushback("profile", dec, draft, jd(), model="m", client=fake_claude())
    assert agree is None


# -- gpt_writer (same interface) -------------------------------------------- #

def test_gpt_writer_returns_validated_draft():
    d = gpt_writer.write_section("profile", "old text", jd(), rubric(), budget(),
                                 version=2, client=fake_gpt("Sharp EMEA pre-sales lead.", [item("minor")]))
    assert isinstance(d, WriterDraft) and d.writer == "gpt" and d.version == 2
    assert d.items[0].source_writer == "gpt" and d.items[0].severity == "minor"


def test_gpt_writer_length_minor_under_budget():
    d = gpt_writer.write_section("profile", "x", jd(), rubric(), budget(),
                                 version=1, client=fake_gpt("two words", []))   # 2 << 5*0.7
    assert any(i.severity == "minor" and "below" in i.issue and i.source_writer == "gpt" for i in d.items)


def test_gpt_writer_raises_on_unparseable():
    from tailor.tools.gpt_writer import WriterError as GWError
    with pytest.raises(GWError):
        gpt_writer.write_section("profile", "x", jd(), rubric(), budget(),
                                 version=1, client=fake_gpt(raw="not json"))


def test_gpt_writer_pushback():
    dec = types.SimpleNamespace(selected_base="claude", gpt_quality=5.0, direction="add metrics")
    draft = WriterDraft("gpt", "profile", "txt", 1)
    assert gpt_writer.pushback("profile", dec, draft, jd(), client=fake_gpt(pushback="metrics aren't in source")) \
        == "metrics aren't in source"
    assert gpt_writer.pushback("profile", dec, draft, jd(), client=fake_gpt()) is None
