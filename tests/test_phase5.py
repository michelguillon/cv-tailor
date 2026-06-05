"""Phase 5 formatting-validation tests. Length check is deterministic; Haiku mocked."""

import types

from tailor.models import SectionBudget
from tailor.phases.phase5_validation import (apply_corrections, assembled_length_check,
                                             render_corrections, validate_formatting)
from tailor.run_context import RunContext


def man(static, version, stype, wc):
    return {"static": static, "version": version, "section_type": stype, "word_count": wc,
            "position": 0, "title": stype, "source_cv": "X", "path": ""}


def fake_haiku(corrected, corrections):
    def create(**kwargs):
        inp = {"corrected_text": corrected, "corrections": corrections}
        block = types.SimpleNamespace(type="tool_use", name="submit_formatting", input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=5, output_tokens=5))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def test_validate_formatting_returns_only_changed(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("profile", "Led teams from 2019-2021.", version=1)
    manifest = {"profile": man(False, 1, "profile", 4),
                "interests": man(True, None, "interests", 3)}
    ctx.write_section("interests", "cycling", static=True)
    corr = validate_formatting(ctx, manifest, model="m",
                               client=fake_haiku("Led teams from 2019–2021.", ["en-dash for date range"]))
    assert set(corr) == {"profile"}                # static skipped; only changed sections
    assert corr["profile"]["corrections"] == ["en-dash for date range"]


def test_validate_formatting_skips_when_no_change(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("profile", "Already clean.", version=1)
    manifest = {"profile": man(False, 1, "profile", 2)}
    corr = validate_formatting(ctx, manifest, model="m", client=fake_haiku("Already clean.", []))
    assert corr == {}


def test_assembled_length_check_over_budget():
    manifest = {"profile": man(False, 1, "profile", 200), "skills": man(False, 0, "skills", 100),
                "interests": man(True, None, "interests", 30)}
    budgets = {"profile": SectionBudget("profile", 50, 120, 90),
               "skills": SectionBudget("skills", 30, 80, 60),
               "interests": SectionBudget("interests", 10, 40, 25)}
    out = assembled_length_check(manifest, budgets)
    assert out["total_words"] == 330 and out["budget_words"] == 240
    assert out["over_budget"] is True
    assert out["longest"][0] == ("profile", 200)    # longest non-static first


def test_apply_corrections_writes_new_version(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("profile", "old", version=1)
    manifest = {"profile": man(False, 1, "profile", 1)}
    corrections = {"profile": {"original": "old", "corrected": "new clean text", "corrections": ["x"]}}
    applied = apply_corrections(ctx, corrections, manifest)
    assert applied == ["profile"] and manifest["profile"]["version"] == 2
    assert ctx.read_section("profile", version=2).strip() == "new clean text"
    assert manifest["profile"]["word_count"] == 3


def test_render_corrections_readable():
    corrections = {"profile": {"original": "a", "corrected": "b", "corrections": ["fixed dash"]}}
    length = {"total_words": 300, "budget_words": 240, "over_budget": True, "longest": [("profile", 200)]}
    out = render_corrections(corrections, length)
    assert "fixed dash" in out and "OVER" in out and "[y] yes" in out
