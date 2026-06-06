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
        "position": 0, "title": "profile", "label": "profile",
    }
    assert manifest["interests"]["section_type"] == "interests"


def test_experience_label_disambiguates_role_at_same_company(tmp_path):
    """Two role-groups at one employer get distinct labels (company — role); the
    CV heading stays the company alone (role is in the body). See memory/F-23."""
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)

    def exp(section_id, role):
        s = sec("AI", section_id, "experience", f"{role} bullets")
        s["company"] = "AppNexus / Xandr"
        s["title"] = role
        return s

    sections = [exp("experience_appnexus_director", "Director, Solutions Engineering"),
                exp("experience_appnexus_consultant", "Solution Consultant")]
    recommended = {"experience_appnexus_director": rec("experience_appnexus_director", "AI"),
                   "experience_appnexus_consultant": rec("experience_appnexus_consultant", "AI")}
    manifest = draft_sections(fit(recommended), jd(), rubric(), sections, budgets(), ctx,
                              model="m", client=fake_claude())

    d = manifest["experience_appnexus_director"]
    c = manifest["experience_appnexus_consultant"]
    assert d["title"] == "AppNexus / Xandr" and c["title"] == "AppNexus / Xandr"     # heading: company
    assert d["label"] == "AppNexus / Xandr — Director, Solutions Engineering"        # disambiguated
    assert c["label"] == "AppNexus / Xandr — Solution Consultant"
    assert d["label"] != c["label"]


def test_experience_role_line_split_out_and_stored(tmp_path):
    """The leading role/date line of an experience section is kept OUT of the
    drafted body (the LLM never sees it, so it can't drop it — F-29) and stored
    verbatim in the manifest for deterministic re-attachment at assembly."""
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    doc = "Senior Product Manager (Apr 2022 – Mar 2024)\n- Built X\n- Shipped Y"
    s = sec("AI", "experience_ms", "experience", doc)
    s["company"] = "Microsoft"; s["title"] = "Senior Product Manager"
    bud = {"experience": SectionBudget("experience", 23, 187, 108)}
    manifest = draft_sections(fit({"experience_ms": rec("experience_ms", "AI")}), jd(), rubric(),
                              [s], bud, ctx, model="m",
                              client=fake_claude("- Built X for EMEA\n- Shipped Y"))
    assert manifest["experience_ms"]["role_line"] == "Senior Product Manager (Apr 2022 – Mar 2024)"
    body = ctx.read_section("experience_ms", version=0)
    assert "Senior Product Manager" not in body          # role line is not in the draftable body


def test_nonexperience_section_has_no_role_line_key(tmp_path):
    """Only experience sections carry role_line (keeps the manifest contract tight)."""
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = draft_sections(fit({"profile": rec("profile", "AI")}), jd(), rubric(),
                              [sec("AI", "profile", "profile", "Original profile")], budgets(), ctx,
                              model="m", client=fake_claude())
    assert "role_line" not in manifest["profile"]


def test_manifest_checkpoint_written(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    sections = [sec("AI", "profile", "profile", "p")]
    draft_sections(fit({"profile": rec("profile", "AI")}), jd(), rubric(), sections, budgets(), ctx,
                   model="m", client=fake_claude())
    assert (ctx.output_dir / "phase2_draft_manifest.json").exists()
    assert (ctx.output_dir / "run_log.jsonl").exists()


def test_cvcm_passed_to_drafting_prompt(tmp_path):
    """When a value model is provided it reaches the Phase-2 drafting prompt, with the
    framing-only guardrail; when absent, no CVCM block appears (§3.9/D-33)."""
    seen = {}

    def capturing_claude():
        def create(**kwargs):
            seen["user"] = kwargs["messages"][0]["content"]
            block = types.SimpleNamespace(type="text", text="Drafted across EMEA.")
            return types.SimpleNamespace(content=[block],
                                         usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))
        return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    args = (fit({"profile": rec("profile", "AI")}), jd(), rubric(),
            [sec("AI", "profile", "profile", "Original profile")], budgets(), ctx)
    draft_sections(*args, model="m", client=capturing_claude(), cvcm="I scale technical teams.")
    assert "CANDIDATE VALUE MODEL" in seen["user"] and "I scale technical teams." in seen["user"]
    assert "BACKGROUND ONLY" in seen["user"]                  # the framing-only guardrail (F-36)

    draft_sections(*args, model="m", client=capturing_claude())   # no cvcm
    assert "CANDIDATE VALUE MODEL" not in seen["user"]


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
