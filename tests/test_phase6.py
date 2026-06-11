"""Phase 6 output-generation tests (deterministic, no API). Step 7."""

from pathlib import Path

import pytest

from tailor.models import FitAssessment, IterationScore, JDAnalysis, ScoringRubric, SectionScore
from tailor.phases.phase6_output import assemble_markdown, generate_output, summary_card
from tailor.run_context import RunContext

CONFIG = {"cv_sections": ["header", "profile", "skills", "experience", "ai_projects",
                          "education", "languages", "certifications", "interests"]}


def man(static, version, stype, position, title, wc):
    return {"static": static, "version": version, "section_type": stype, "position": position,
            "title": title, "word_count": wc, "source_cv": "X", "path": ""}


def setup(ctx):
    ctx.write_section("header", "Michel Guillon\nlondon · email@x.com", static=True)
    ctx.write_section("profile", "Original profile alpha", version=0)
    ctx.write_section("profile", "Tailored profile alpha beta", version=1)
    ctx.write_section("skills", "- python\n- aws", version=0)
    ctx.write_section("experience_acme", "Led delivery at Acme", version=0)
    return {
        "header": man(True, None, "header", 0, "header", 2),
        "profile": man(False, 1, "profile", 1, "Profile", 4),
        "skills": man(False, 0, "skills", 2, "Core Skills", 2),
        "experience_acme": man(False, 0, "experience", 3, "Acme", 4),
    }


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], [], "payments", ["technical"])


def iters():
    ss = {
        "profile": SectionScore("profile", "profile", 0.5, claude_quality=8.0, gpt_quality=6.0,
                                selected_writer="claude", converged=True, current_version=1),
        "skills": SectionScore("skills", "skills", 0.5, claude_quality=7.0, gpt_quality=7.0,
                               selected_writer="synthesis", converged=True, current_version=0),
        "experience_acme": SectionScore("experience_acme", "experience", 0.4, claude_quality=6.0,
                                        gpt_quality=6.5, selected_writer="gpt", converged=False, current_version=0),
    }
    return [IterationScore(1, 0.6, 7.0, 0.1, 0.0, 2, 1, section_scores=ss)]


def test_assemble_markdown_order_and_headings(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    md = assemble_markdown(ctx, manifest, CONFIG)
    # header first (no heading), then Profile, Skills, Acme — by (type order, position)
    assert md.index("Michel Guillon") < md.index("## Profile") < md.index("## Core Skills") < md.index("## Acme")
    assert "## header" not in md                       # header rendered without a heading
    assert "Tailored profile alpha beta" in md         # latest version used (v1, not v0)


def test_generate_output_writes_md_and_html(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    ctx.audit.log_event("refinement", "section_adjudicated", "profile chose claude", iteration=1)
    fit = FitAssessment(outcome="partial", overall_fit_score=0.74)
    rubric = ScoringRubric(2, ["alpha", "beta"], [], [], "t", "t", [])

    out = generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)

    assert Path(out["md"]).exists() and Path(out["html"]).exists()
    html = Path(out["html"]).read_text(encoding="utf-8")
    # tabs present (Fit added in F-39)
    for tab in ("Fit", "CV", "Changes", "Scores", "Reasoning"):
        assert tab in html
    # profile v0→v1 diff produced an insertion; scores show the selected writer + freeze
    assert "<ins>" in html
    assert "synthesis" in html and "frozen" in html
    # reasoning trace rendered the audit event
    assert "section_adjudicated" in html


def test_fit_tab_renders_value_alignment(tmp_path):
    """The Fit tab shows the CVCM value-alignment narrative + transferable + gaps (F-39)."""
    from tailor.models import FitGap
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(
        outcome="partial", overall_fit_score=0.68,
        skills_transferable=["building operating models"],
        gaps=[FitGap(requirement="payments domain", gap_type="experience", severity="major",
                     addressable=True, reason="no payments work")],
        value_alignment_notes="Your core pattern is turning capability into outcomes.")
    out = generate_output(ctx, manifest, jd(), fit,
                          ScoringRubric(1, ["alpha"], [], [], "t", "t", []), iters(), config=CONFIG)
    html = Path(out["html"]).read_text(encoding="utf-8")
    assert 'data-tab="fit"' in html and 'id="fit" class="tab active"' in html
    assert "Your core pattern is turning capability into outcomes." in html
    assert "building operating models" in html and "payments domain" in html


def test_reasoning_skips_non_reasoning_records(tmp_path):
    """The run_complete cost footer (no phase/event) must NOT render as an empty '?'
    reasoning group (F-40)."""
    from tailor.phases.phase6_output import _build_reasoning
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.audit.log_event("refinement", "section_adjudicated", "x", iteration=1)
    # mimic run.py's footer: a record with neither phase nor event
    with (ctx.output_dir / "run_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"type": "run_complete", "total_estimated_usd": 0.1}\n')

    groups = _build_reasoning(ctx)
    assert groups and all(g["phase"] and g["phase"] != "?" for g in groups)
    assert all(e.get("event") for g in groups for e in g["entries"])


def test_experience_role_line_reattached(tmp_path):
    """The experience role/date line (manifest role_line) is rendered bold between
    the company heading and the body — guarantees the title survives and two
    role-groups at one employer stay distinct (F-29, D-21)."""
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("experience_ms", "- Built X\n- Shipped Y", version=0)
    m = {**man(False, 0, "experience", 3, "Microsoft", 4),
         "role_line": "Senior Product Manager (Apr 2022 – Mar 2024)"}
    md = assemble_markdown(ctx, {"experience_ms": m}, CONFIG)
    assert "## Microsoft" in md
    assert "**Senior Product Manager (Apr 2022 – Mar 2024)**" in md
    assert md.index("**Senior Product Manager") < md.index("- Built X")   # role line before body


def test_md_to_html_renders_bold():
    from tailor.phases.phase6_output import _md_to_html
    assert "<strong>Senior PM (2022)</strong>" in _md_to_html("**Senior PM (2022)**")


def test_static_section_marked_verbatim_in_changes(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="strong", overall_fit_score=0.9)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    out = generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)
    html = Path(out["html"]).read_text(encoding="utf-8")
    assert "copied verbatim" in html


# --------------------------------------------------------------------------- #
# Summary card (D-34) + JD tab (D-37)                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("outcome,fit,grounded,unsup,band,status", [
    ("strong", 0.90, 0.80, 0, "strong", "Submit-ready"),   # strong + 0 flags → submit-ready
    ("strong", 0.90, 0.80, 2, "strong", "Review Required"),  # flags downgrade a strong fit
    ("partial", 0.58, 0.36, 1, "partial", "Review Required"),
    ("partial", 0.58, 0.36, 0, "partial", "Review Required"),  # <75% → review even with 0 flags
    ("no_fit", 0.20, 0.10, 0, "low", "Do Not Submit"),
])
def test_summary_card_status_and_band(outcome, fit, grounded, unsup, band, status):
    card = summary_card(outcome, fit, grounded, unsup)
    assert card["fit_band"] == band and card["status"] == status
    assert card["fit_pct"] == round(fit * 100) and card["grounded_pct"] == round(grounded * 100)
    assert card["unsupported"] == unsup


def test_summary_card_handles_missing_numbers():
    card = summary_card("partial", None, None, 0)
    assert card["fit_pct"] is None and card["grounded_pct"] is None
    assert card["fit_band"] == "low" and card["status"] == "Review Required"


def test_generate_output_renders_summary_card(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="partial", overall_fit_score=0.58)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    # iters() final keyword_coverage is 0.6 → grounded 60%; no verification_flags → 0 unsupported
    out = generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)
    html = Path(out["html"]).read_text(encoding="utf-8")
    assert "Grounded Coverage:" in html and "60%" in html
    assert "Unsupported Claims:" in html
    assert "Status: Review Required" in html
    # CV overall quality (final iteration critique_score = 7.0) shown in the header
    assert "CV Quality:" in html and "7.0/10" in html


def test_header_shows_job_radar_provenance_when_present(tmp_path):
    """A run created from Job Radar (run_meta.json sidecar) shows a 'From Job Radar' badge in the
    report header; a plain run shows nothing (Integration §5.2 / F-51)."""
    import json

    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="partial", overall_fit_score=0.58)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    (ctx.output_dir / "run_meta.json").write_text(json.dumps({
        "job_radar_source": {"company": "Elastic", "fit_label": "strong_fit", "fit_score": 10,
                             "source_url": "https://jobs.example.com/elastic/pm"}}), encoding="utf-8")
    html = Path(generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)["html"]).read_text("utf-8")
    assert "From Job Radar: Elastic" in html and "strong_fit" in html
    assert 'href="https://jobs.example.com/elastic/pm"' in html


def test_header_no_job_radar_badge_for_plain_run(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="partial", overall_fit_score=0.58)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    html = Path(generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)["html"]).read_text("utf-8")
    assert "From Job Radar" not in html


def test_jd_tab_renders_raw_jd(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="strong", overall_fit_score=0.9)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    out = generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG,
                          jd_raw="Director of Solutions Engineering — EMEA. Lead the team.")
    html = Path(out["html"]).read_text(encoding="utf-8")
    assert 'data-tab="jd"' in html
    assert "Director of Solutions Engineering — EMEA. Lead the team." in html


def test_jd_tab_empty_state_without_jd(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx)
    fit = FitAssessment(outcome="strong", overall_fit_score=0.9)
    rubric = ScoringRubric(1, ["alpha"], [], [], "t", "t", [])
    out = generate_output(ctx, manifest, jd(), fit, rubric, iters(), config=CONFIG)  # no jd_raw
    html = Path(out["html"]).read_text(encoding="utf-8")
    assert "JD not recorded for this run." in html
