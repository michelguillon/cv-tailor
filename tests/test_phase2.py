"""Step 4 tests: config/budgets loader, RunContext checkpoints, drafting (mocked)."""

import types

import pytest

from tailor.config import load_budgets
from tailor.models import JDAnalysis, ScoringRubric, SectionBudget, SectionRecommendation
from tailor.phases.phase2_initial_draft import DraftError, draft_sections
from tailor.run_context import RunContext, new_run_id


# --------------------------------------------------------------------------- #
# config / budgets                                                            #
# --------------------------------------------------------------------------- #

def test_load_budgets(tmp_path):
    p = tmp_path / "budgets.yaml"
    p.write_text("profile:\n  min_words: 70\n  max_words: 115\n  target_words: 91\n", encoding="utf-8")
    budgets = load_budgets(p)
    assert isinstance(budgets["profile"], SectionBudget)
    assert budgets["profile"].target_words == 91


def test_load_budgets_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_budgets(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- #
# RunContext                                                                  #
# --------------------------------------------------------------------------- #

def test_new_run_id_format():
    rid = new_run_id()
    assert rid.startswith("run_") and len(rid) == len("run_20260604_142301")


def test_run_context_section_io(tmp_path):
    ctx = RunContext.create(run_id="run_test", base_dir=tmp_path)
    assert ctx.sections_dir.is_dir()
    ctx.write_section("profile", "drafted profile", version=0)
    ctx.write_section("interests", "cycling", static=True)
    assert ctx.section_path("profile", version=0).name == "profile_v0.md"
    assert ctx.section_path("interests", static=True).name == "interests_static.md"
    assert ctx.read_section("profile", version=0).strip() == "drafted profile"
    assert ctx.read_section("interests", static=True).strip() == "cycling"


def test_run_context_requires_version_for_nonstatic(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    with pytest.raises(ValueError):
        ctx.section_path("profile")  # neither version nor static


def test_run_context_checkpoint(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    path = ctx.write_checkpoint("phase2_draft_manifest", {"profile": {"version": 0}})
    assert path.exists() and path.name == "phase2_draft_manifest.json"


# --------------------------------------------------------------------------- #
# draft_sections (mocked Claude)                                              #
# --------------------------------------------------------------------------- #

def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], ["fintech"], "payments", ["technical"])


def rubric():
    return ScoringRubric(1, ["pre-sales", "emea"], [], [], "t", "t", [])


def fit(recommended):
    return types.SimpleNamespace(recommended_sections=recommended)


def sec(short_cv, section_id, section_type, document, static=False):
    return {
        "filename": f"CV_Michel_Guillon_2026_{short_cv}.docx",
        "section_id": section_id, "section_type": section_type,
        "document": document, "static": static, "company": "", "version_date": "2026-01-01",
    }


def rec(section_id, source_cv, reason="best"):
    return SectionRecommendation(section_id, source_cv, "2026-01-01", 0.3, reason)


def fake_claude(text="Tailored pre-sales leader across EMEA payments."):
    def create(**kwargs):
        block = types.SimpleNamespace(type="text", text=text)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=50, output_tokens=30))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def budgets():
    return {"profile": SectionBudget("profile", 70, 115, 91)}


def test_drafts_nonstatic_and_copies_static(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    sections = [
        sec("Figma", "profile", "profile", "Original profile text"),
        sec("Figma", "interests", "interests", "Cycling, woodworking", static=True),
    ]
    recommended = {"profile": rec("profile", "Figma"), "interests": rec("interests", "Figma", "static — base")}
    manifest = draft_sections(fit(recommended), jd(), rubric(), sections, budgets(), ctx,
                              model="m", client=fake_claude())

    # static copied verbatim
    assert ctx.read_section("interests", static=True).strip() == "Cycling, woodworking"
    assert manifest["interests"]["static"] is True
    # non-static drafted (from the mock), v0 written
    assert "EMEA" in ctx.read_section("profile", version=0)
    assert manifest["profile"] == {
        "static": False, "version": 0, "word_count": 6, "source_cv": "Figma",
        "path": str(ctx.section_path("profile", version=0)), "section_type": "profile",
        "position": 0, "title": "profile",
    }
    assert manifest["interests"]["section_type"] == "interests"


def test_manifest_checkpoint_written(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    sections = [sec("AI", "profile", "profile", "p")]
    draft_sections(fit({"profile": rec("profile", "AI")}), jd(), rubric(), sections, budgets(), ctx,
                   model="m", client=fake_claude())
    assert (ctx.output_dir / "phase2_draft_manifest.json").exists()
    assert (ctx.output_dir / "run_log.jsonl").exists()


def test_no_fit_raises(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    with pytest.raises(DraftError):
        draft_sections(fit(None), jd(), rubric(), [], budgets(), ctx, model="m", client=fake_claude())


def test_missing_source_raises(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    recommended = {"profile": rec("profile", "Nonexistent")}
    with pytest.raises(DraftError):
        draft_sections(fit(recommended), jd(), rubric(), [sec("AI", "profile", "profile", "p")],
                       budgets(), ctx, model="m", client=fake_claude())
