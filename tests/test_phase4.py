"""Phase 4 HITL tests. render is deterministic; interpret/revise use mocked Claude."""

import types

import pytest

from tailor.models import (CritiqueItem, IterationScore, JDAnalysis, ScoringRubric,
                           SectionBudget, SectionScore)
from tailor.phases import phase4_hitl
from tailor.phases.phase3_refinement import LoopMemory, RefinementResult
from tailor.phases.phase4_hitl import (HITLError, interpret_freetext, render_section_review,
                                       revise_section, unresolved_list)
from tailor.run_context import RunContext


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], [], "payments", ["technical"])


def man(static, version, stype, title):
    return {"static": static, "version": version, "section_type": stype, "title": title,
            "position": 0, "word_count": 5, "source_cv": "X", "path": ""}


def result(unresolved=None):
    manifest = {
        "profile": man(False, 2, "profile", "Profile"),
        "skills": man(False, 3, "skills", "Core Skills"),
        "interests": man(True, None, "interests", "Interests"),
    }
    ss = {
        "profile": SectionScore("profile", "profile", 0.8, claude_quality=8.0, gpt_quality=7.0,
                                selected_writer="claude", converged=True, current_version=2),
        "skills": SectionScore("skills", "skills", 0.6, claude_quality=6.0, gpt_quality=6.0,
                               selected_writer="gpt", converged=False, current_version=3),
    }
    its = [IterationScore(1, 0.6, 7.0, 0.1, 0.0, 1, 1, section_scores=ss)]
    return RefinementResult(iterations=its, final_rubric=ScoringRubric(1, [], [], [], "t", "t", []),
                            converged=False, convergence_reason="max_iterations",
                            manifest=manifest, memory=LoopMemory(),
                            unresolved=unresolved or {})


def test_render_shows_status_and_unresolved():
    r = result({"skills": [CritiqueItem("skills", "minor", "quantify team size", "add a number", "gpt")]})
    out = render_section_review(r, max_iterations=3)
    assert "Refinement complete" in out
    assert "converged iter 1" in out          # profile converged
    assert "did not converge" in out          # skills active
    assert "static" in out                    # interests
    assert "Unresolved items (1)" in out and "[1] Core Skills: \"quantify team size\"" in out


def test_unresolved_list_flattens():
    r = result({"skills": [CritiqueItem("skills", "minor", "a", "b", "gpt"),
                           CritiqueItem("skills", "major", "c", "d", "claude")]})
    assert len(unresolved_list(r)) == 2


def fake_claude(*, section_id=None, instruction="do it", draft_text=None, bad=False):
    """Dispatch: interpret_revision (Phase 4) and submit_draft (claude_writer)."""
    def create(**kwargs):
        name = kwargs["tool_choice"]["name"]
        if name == "interpret_revision":
            inp = {"section_id": ("ghost" if bad else section_id), "instruction": instruction}
        else:  # submit_draft
            inp = {"text": draft_text or "revised skills text", "items": []}
        block = types.SimpleNamespace(type="tool_use", name=name, input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=5, output_tokens=5))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def test_interpret_freetext_maps_to_section():
    r = result()
    out = interpret_freetext("make the skills section punchier", r, model="m",
                             client=fake_claude(section_id="skills", instruction="make it punchier"))
    assert out == {"section_id": "skills", "instruction": "make it punchier"}


def test_interpret_freetext_raises_on_unknown_section():
    r = result()
    with pytest.raises(HITLError):
        interpret_freetext("do something", r, model="m", client=fake_claude(bad=True))


def test_revise_section_writes_new_version(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("skills", "old skills v3", version=3)
    r = result()
    budgets = {"skills": SectionBudget("skills", 1, 50, 10)}
    version, text = revise_section("skills", "make it punchier", r, jd(), r.final_rubric, budgets, ctx,
                                   model="m", client=fake_claude(draft_text="punchier skills"))
    assert version == 4 and r.manifest["skills"]["version"] == 4
    assert ctx.read_section("skills", version=4).strip() == "punchier skills"


def test_revise_static_section_rejected(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    r = result()
    with pytest.raises(HITLError):
        revise_section("interests", "x", r, jd(), r.final_rubric, {}, ctx, model="m", client=fake_claude())
