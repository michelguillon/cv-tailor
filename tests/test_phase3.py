"""Phase 3 dual-writer refinement loop tests (D-28), mocked providers — no API.

(a) single-iteration mechanics: valid per-section IterationScore, selected text +
per-writer drafts on disk; (b) freezing excludes a section from later iterations;
dual-signal convergence; rejected-minor forwarding; rubric extension. Freeze logic
is deterministic: same input → same freeze decision.

The mocks are prompt-aware — they parse the section id from the writer/orchestrator
prompts so a single mock serves every section with per-section config.
"""

import re
import types

from tailor.models import IterationScore, JDAnalysis, ScoringRubric, SectionBudget, SectionScore
from tailor.phases.phase3_refinement import RefinementResult, refine
from tailor.run_context import RunContext


def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA"], ["fintech"], "payments", ["technical"])


def rubric(required=("alpha", "beta")):
    return ScoringRubric(1, list(required), ["fintech"], [], "t", "t", [])


def budgets():
    return {t: SectionBudget(t, 1, 50, 10) for t in ("profile", "skills")}


def _prompt(kwargs):
    msgs = kwargs.get("messages", [])
    return " ".join(m["content"] for m in msgs if isinstance(m.get("content"), str))


def decision(base="claude", cq=8.0, gq=6.0, converged=False, additions=()):
    return {"selected_base": base, "final_text": "", "direction": "tighten opening",
            "synthesis_notes": None, "claude_quality": cq, "gpt_quality": gq,
            "converged": converged, "rubric_additions": list(additions)}


def fake_claude(cfg, rubric_decisions=()):
    """Anthropic mock serving claude_writer, orchestrator, pushback, rubric — by tool."""
    rub = list(rubric_decisions)

    def create(**kwargs):
        name = kwargs["tool_choice"]["name"]
        p = _prompt(kwargs)
        if name == "submit_draft":
            sid = re.search(r"SECTION TYPE: (\S+)", p).group(1)
            inp = {"text": f"{sid} alpha beta", "items": cfg[sid].get("claude_items", [])}
        elif name == "submit_decision":
            sid = re.search(r"--- CLAUDE DRAFT[^\n]*---\s*(\S+)", p).group(1)
            inp = cfg[sid]["decision"]
        elif name == "submit_pushback":
            inp = {"disagree": False, "reasoning": ""}
        elif name == "revise_direction":
            inp = {"direction": "hold", "revised": False, "reasoning": "r"}
        elif name == "submit_rubric_decisions":
            inp = {"decisions": rub.pop(0) if rub else []}
        block = types.SimpleNamespace(type="tool_use", name=name, input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=10))
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def fake_gpt(cfg):
    import json

    def create(**kwargs):
        fmt = kwargs["response_format"]["json_schema"]["name"]
        p = _prompt(kwargs)
        if fmt == "writer_draft":
            sid = re.search(r"SECTION TYPE: (\S+)", p).group(1)
            content = json.dumps({"text": f"{sid} alpha", "items": cfg[sid].get("gpt_items", [])})
        else:
            content = json.dumps({"disagree": False, "reasoning": ""})
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=10))
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def cit(sev, sid="profile"):
    return {"severity": sev, "issue": f"{sev} issue", "suggestion": f"{sev} suggestion"}


def setup(ctx, specs):
    """specs: {sid: (section_type, static)}. v0 non-static = '{sid} alpha' (coverage 0.5)."""
    manifest = {}
    for sid, (stype, static) in specs.items():
        if static:
            ctx.write_section(sid, "static text", static=True)
            v = None
        else:
            ctx.write_section(sid, f"{sid} alpha", version=0)
            v = 0
        manifest[sid] = {"static": static, "version": v, "source_cv": "X",
                         "path": "", "section_type": stype, "word_count": 2}
    return manifest


# --------------------------------------------------------------------------- #
# (a) single-iteration mechanics                                              #
# --------------------------------------------------------------------------- #

def test_single_iteration_dual_writer(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False), "interests": ("interests", True)})
    cfg = {"profile": {"claude_items": [cit("major")], "gpt_items": [],
                       "decision": decision(base="claude", cq=8.0, gq=6.0, converged=False)}}

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt(cfg))

    assert isinstance(res, RefinementResult) and len(res.iterations) == 1
    it = res.iterations[0]
    assert isinstance(it, IterationScore) and it.iteration == 1
    assert set(it.section_scores) == {"profile"}            # static excluded (D-13)
    ps = it.section_scores["profile"]
    assert isinstance(ps, SectionScore)
    assert ps.claude_quality == 8.0 and ps.gpt_quality == 6.0 and ps.selected_writer == "claude"
    # selected = claude draft text written as v1; per-writer drafts also on disk
    assert ctx.read_section("profile", version=1).strip() == "profile alpha beta"
    assert (ctx.sections_dir / "profile_claude_v1.md").exists()
    assert (ctx.sections_dir / "profile_gpt_v1.md").exists()
    assert manifest["profile"]["version"] == 1
    # one major item present → not frozen → stops at the hard cap
    assert res.convergence_reason == "max_iterations" and ps.converged is False


def test_synthesis_selection_writes_merged_text(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    dec = {"selected_base": "synthesis", "final_text": "profile alpha beta synthesised",
           "direction": "d", "synthesis_notes": "take both", "claude_quality": 7.0,
           "gpt_quality": 7.0, "converged": False, "rubric_additions": []}
    cfg = {"profile": {"claude_items": [cit("major")], "gpt_items": [], "decision": dec}}

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt(cfg))

    assert ctx.read_section("profile", version=1).strip() == "profile alpha beta synthesised"
    assert res.iterations[0].section_scores["profile"].selected_writer == "synthesis"


# --------------------------------------------------------------------------- #
# (b) freezing + convergence + memory                                         #
# --------------------------------------------------------------------------- #

def test_freeze_excludes_section_from_later_iteration(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False), "skills": ("skills", False)})
    cfg = {
        "profile": {"claude_items": [], "gpt_items": [],   # zero major + converged → freeze iter1
                    "decision": decision(base="claude", cq=8.0, gq=8.0, converged=True)},
        "skills": {"claude_items": [cit("major")], "gpt_items": [],  # major → never frozen
                   "decision": decision(base="claude", cq=8.0, gq=8.0, converged=False)},
    }
    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=2,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt(cfg))

    # profile froze at iter1 → no v2; skills written both iterations
    assert res.iterations[0].section_scores["profile"].converged is True
    assert res.iterations[0].sections_converged == 1
    assert manifest["profile"]["version"] == 1 and not (ctx.sections_dir / "profile_v2.md").exists()
    assert manifest["skills"]["version"] == 2 and (ctx.sections_dir / "skills_v2.md").exists()
    # iter2 only adjudicated skills → no second claude draft for profile
    assert not (ctx.sections_dir / "profile_claude_v2.md").exists()
    assert (ctx.sections_dir / "skills_claude_v2.md").exists()


def test_dual_signal_convergence(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    # constant decision + major item (never frozen): coverage/quality stable → Δ→0 at iter2
    cfg = {"profile": {"claude_items": [cit("major")], "gpt_items": [],
                       "decision": decision(base="claude", cq=7.0, gq=6.0, converged=False)}}
    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=3,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt(cfg))

    assert len(res.iterations) == 2 and res.converged is True
    assert res.convergence_reason == "dual_signal_converged"
    assert abs(res.iterations[1].keyword_delta) < 0.05
    assert abs(res.iterations[1].quality_delta) < 0.5


def test_rejected_minor_suggestion_forwarded(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    cfg = {"profile": {"claude_items": [cit("minor")], "gpt_items": [cit("major")],
                       "decision": decision(converged=False)}}
    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt(cfg))

    # minor suggestion carried into loop memory; major NOT (must stay raisable)
    assert "minor suggestion" in res.memory.rejected_suggestions
    assert "major suggestion" not in res.memory.rejected_suggestions


# --------------------------------------------------------------------------- #
# (c) graceful degradation on a writer failure (F-39)                          #
# --------------------------------------------------------------------------- #

def _failing_create(**kwargs):
    raise RuntimeError("simulated provider outage")


def fake_gpt_failing():
    """A GPT client whose every call raises — simulates a transient provider outage."""
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_failing_create)))


def test_gpt_failure_degrades_to_claude_only(tmp_path):
    """A GPT writer failure must NOT abort the run: the loop proceeds with the Claude
    draft (verbatim, not a synthesised reword) and the section is still produced (F-39)."""
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    # orchestrator would synthesise a reword — degradation must override to the survivor
    # (claude) verbatim, since there's only one real draft to merge.
    dec = {"selected_base": "synthesis", "final_text": "profile MERGED reword", "direction": "d",
           "synthesis_notes": "n", "claude_quality": 8.0, "gpt_quality": 8.0,
           "converged": False, "rubric_additions": []}
    cfg = {"profile": {"claude_items": [], "gpt_items": [], "decision": dec}}

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
                 claude_client=fake_claude(cfg), openai_client=fake_gpt_failing())

    assert len(res.iterations) == 1                       # ran to completion, no crash
    ps = res.iterations[0].section_scores["profile"]
    assert ps.selected_writer == "claude"                # forced to survivor, not "synthesis"
    # claude text verbatim — NOT the synthesised reword (no drift on the degraded path)
    assert ctx.read_section("profile", version=1).strip() == "profile alpha beta"
    events = [__import__("json").loads(l)
              for l in (ctx.output_dir / "run_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "writer_degraded" for e in events)


def test_both_writers_failing_surfaces(tmp_path):
    """If BOTH writers fail there is nothing to draft — surface it (R-09), don't ship blank."""
    import pytest
    from tailor.tools.gpt_writer import WriterError
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    failing_claude = types.SimpleNamespace(messages=types.SimpleNamespace(create=_failing_create))

    with pytest.raises(WriterError):
        refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
               claude_client=failing_claude, openai_client=fake_gpt_failing())


def test_rubric_extended_during_loop(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup(ctx, {"profile": ("profile", False)})
    cfg = {"profile": {"claude_items": [cit("major")], "gpt_items": [],
                       "decision": decision(additions=["gamma"])}}
    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m", max_iterations=1,
                 claude_client=fake_claude(cfg, rubric_decisions=[[{"keyword": "gamma", "implied_by_jd": True, "reason": "JD implies it"}]]),
                 openai_client=fake_gpt(cfg))

    assert res.final_rubric.version == 2 and "gamma" in res.final_rubric.required_keywords
