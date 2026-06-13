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


# -- structure preservation (F-56) ------------------------------------------ #
#
# The real "rendering bug": the writers flattened bulleted experience and the
# "·"-delimited skills list into prose, so the CV tab rendered walls of text.
# `structure_preserved()` catches it deterministically (list markers, not the
# model's self-report); the writers set the flag on every WriterDraft.

# The Utiq experience section from run_20260613_111401 — a bulleted SOURCE the
# writer flattened into one prose paragraph at v0 (the observed defect).
UTIQ_SOURCE = (
    "- Joined at a pre-commercial stage to define and establish the UK solutions consulting "
    "function, including operating model, engagement approach, and integration frameworks "
    "across publishers, platforms, and CTV partners.\n"
    "- Worked directly with C-level clients and senior stakeholders to shape go-to-market "
    "strategy, while leading the design of Utiq's identity graph solution.\n"
    "- Activated initial pilot campaigns across multiple publisher partners, achieving 40% "
    "incremental audience reach and informing the broader commercialisation roadmap.\n"
    "- Designed and deployed an AI-powered RFI response platform: a production-ready RAG "
    "pipeline using Mistral and ChromaDB with hybrid retrieval and reranking."
)
UTIQ_FLATTENED = (
    "Joined at pre-commercial stage to define and establish the UK solutions consulting "
    "function across publishers, platforms, and CTV partners. Worked directly with C-level "
    "clients to shape go-to-market strategy while leading the design of Utiq's identity graph "
    "solution. Activated initial pilot campaigns achieving 40% incremental audience reach. "
    "Designed and deployed an AI-powered RFI response platform using Mistral and ChromaDB."
)
SKILLS_SOURCE = ("Solutions Engineering Leadership · Technical Pre-Sales · Solution Architecture · "
                 "Executive Engagement · API & Platform Integrations · GTM Strategy")


def test_structure_preserved_flags_flattened_bullets():
    """A bulleted source flattened to a prose paragraph fails the check (the Utiq defect)."""
    from tailor.tools.writer_common import structure_preserved
    assert structure_preserved(UTIQ_SOURCE, UTIQ_FLATTENED) is False
    # the same source kept as bullets passes
    kept = "\n".join("- " + ln.lstrip("- ") for ln in UTIQ_SOURCE.splitlines())
    assert structure_preserved(UTIQ_SOURCE, kept) is True


def test_structure_preserved_flags_skills_list_turned_to_prose():
    """A "·"-delimited skills list rewritten as sentences fails ("skills literal" defect)."""
    from tailor.tools.writer_common import structure_preserved
    prose = "Solutions architecture and technical pre-sales across enterprise deals."
    assert structure_preserved(SKILLS_SOURCE, prose) is False
    reordered = "Solution Architecture · Technical Pre-Sales · GTM Strategy · Executive Engagement"
    assert structure_preserved(SKILLS_SOURCE, reordered) is True


def test_structure_preserved_prose_source_unconstrained():
    """A prose source imposes no structure constraint — prose in, prose out is fine."""
    from tailor.tools.writer_common import structure_preserved
    assert structure_preserved("A prose profile paragraph with no lists.", "Another prose paragraph.") is True


def test_claude_writer_sets_structure_preserved_false_on_flatten():
    """The writer records structure_preserved on the draft from the deterministic check —
    a flattened bulleted source ⇒ False (claude_writer, F-56)."""
    d = claude_writer.write_section("experience_utiq", UTIQ_SOURCE, jd(), rubric(), budget(),
                                    version=1, model="m", client=fake_claude(UTIQ_FLATTENED, []))
    assert d.structure_preserved is False


def test_gpt_writer_sets_structure_preserved_true_when_bullets_kept():
    """Bullets in, bullets out ⇒ structure_preserved True (gpt_writer, F-56)."""
    bulleted = "- Drove 20% YoY growth across Tier 1 accounts.\n- Co-built a Chrome extension retaining $500k."
    d = gpt_writer.write_section("experience_utiq", UTIQ_SOURCE, jd(), rubric(), budget(),
                                 version=1, client=fake_gpt(bulleted, []))
    assert d.structure_preserved is True


# -- deterministic structure BACKSTOP (F-56) -------------------------------- #
#
# When BOTH writers flatten a bulleted source (the Haiku/demo case the prompt rule
# can't fix), enforce_source_structure rebuilds the bullets from the prose — pure
# reformatting, no wording change. Fixture is the real flattened Microsoft section
# from run_20260613_125435 (4 source bullets → one prose paragraph in both drafts).

MICROSOFT_FLATTENED = (
    "Initiated cross-device graph offering redesign, achieving an 8x increase in audience "
    "expansions into CTV and reducing infrastructure costs by $300k annually. Scoped and "
    "delivered Real-Time Data Provider capability as part of a cross-functional product team, "
    "contributing to $5M in secured revenue. Partnered with engineering to restructure identity "
    "processing architecture, migrating to owned data centres to reduce cost and operational "
    "risk while enabling new product capabilities. Took ownership of a stalled publisher "
    "identity management feature, leading a full redesign of the API and UI to align with "
    "evolving client privacy and control requirements."
)


def test_enforce_source_structure_rebuilds_bullets_without_changing_words():
    """A flattened bulleted source is split back into bullets; no word is added or dropped."""
    from tailor.tools.writer_common import enforce_source_structure
    out = enforce_source_structure(UTIQ_SOURCE, MICROSOFT_FLATTENED)
    lines = out.splitlines()
    assert len(lines) == 4 and all(ln.startswith("- ") for ln in lines)   # 4 source bullets recovered
    # the backstop only inserts "- " + newlines — words are identical to the prose
    assert " ".join(ln[2:] for ln in lines).split() == MICROSOFT_FLATTENED.split()
    # $300k / $5M / API / UI did not cause spurious splits
    assert any("$5M in secured revenue." in ln for ln in lines)


def test_enforce_source_structure_leaves_good_input_untouched():
    """No-op when the draft already has bullets, the source is prose, or it's a skills list."""
    from tailor.tools.writer_common import enforce_source_structure
    already = "- one\n- two"
    assert enforce_source_structure(UTIQ_SOURCE, already) == already        # already bulleted
    assert enforce_source_structure("Prose source.", "Prose draft.") == "Prose draft."  # prose source
    # a "·"-skills list flattened to prose is NOT reconstructable → left for the model path
    assert enforce_source_structure(SKILLS_SOURCE, "Skills as a sentence.") == "Skills as a sentence."
