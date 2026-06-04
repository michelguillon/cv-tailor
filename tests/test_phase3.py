"""Phase 3 refinement-loop tests (mocked Claude + GPT, no API). Step 6.

Two checkpoints, mirroring the build: (a) single-iteration mechanics produce a
valid per-section IterationScore and version files; (b) multi-iteration freezing
(frozen sections excluded from later critique) and dual-signal convergence.
Section-freeze logic is deterministic: same input → same freeze decision.
"""

import json
import re
import types

import pytest

from tailor.models import IterationScore, JDAnalysis, ScoringRubric, SectionBudget, SectionScore
from tailor.phases.phase3_refinement import RefinementResult, refine
from tailor.run_context import RunContext


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                            #
# --------------------------------------------------------------------------- #

def jd():
    return JDAnalysis("...", "Director, SE", "director", ["lead EMEA", "presales"], ["fintech"], "payments", ["technical"])


def rubric(required=("alpha", "beta")):
    return ScoringRubric(1, list(required), ["fintech"], [], "t", "t", [])


def budgets():
    return {"profile": SectionBudget("profile", 1, 50, 10), "skills": SectionBudget("skills", 1, 50, 10)}


def fake_openai(*payloads):
    """Critique client: canned JSON per call; records the sections each call saw."""
    state = {"n": 0, "seen": []}

    def create(**kwargs):
        payload = payloads[min(state["n"], len(payloads) - 1)]
        state["n"] += 1
        blob = " ".join(m["content"] for m in kwargs["messages"])
        state["seen"].append(set(re.findall(r"SECTION:\s*(\S+)", blob)))
        content = payload if isinstance(payload, str) else json.dumps(payload)
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=10))

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client.state = state
    return client


def fake_claude(decision_sets=(), revision_texts=(), rubric_decision_sets=()):
    """One Claude mock serving all three roles, dispatched by tool_choice.

    submit_decisions → next decision_set; submit_rubric_decisions → next rubric
    set; no tool (a revision) → next revision_text.
    """
    q = {"dec": list(decision_sets), "rev": list(revision_texts), "rub": list(rubric_decision_sets)}

    def create(**kwargs):
        tc = kwargs.get("tool_choice") or {}
        name = tc.get("name")
        if name == "submit_decisions":
            decisions = q["dec"].pop(0) if q["dec"] else []
            block = types.SimpleNamespace(type="tool_use", name=name, input={"decisions": decisions})
        elif name == "submit_rubric_decisions":
            decisions = q["rub"].pop(0) if q["rub"] else []
            block = types.SimpleNamespace(type="tool_use", name=name, input={"decisions": decisions})
        else:
            text = q["rev"].pop(0) if q["rev"] else "alpha beta revised"
            block = types.SimpleNamespace(type="text", text=text)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=10))

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def setup_sections(ctx, specs):
    """specs: {sid: (section_type, static, text)}. Writes v0/static; returns manifest."""
    manifest = {}
    for sid, (stype, static, text) in specs.items():
        if static:
            ctx.write_section(sid, text, static=True)
            manifest[sid] = {"static": True, "version": None, "source_cv": "X",
                             "path": str(ctx.section_path(sid, static=True)),
                             "section_type": stype, "word_count": len(text.split())}
        else:
            ctx.write_section(sid, text, version=0)
            manifest[sid] = {"static": False, "version": 0, "source_cv": "X",
                             "path": str(ctx.section_path(sid, version=0)),
                             "section_type": stype, "word_count": len(text.split())}
    return manifest


def crit(score, scores: dict, items: list, additions=()):
    return {"overall_score": score,
            "section_scores": [{"section": s, "score": v} for s, v in scores.items()],
            "items": items, "rubric_additions": list(additions)}


def item(section, severity, issue="issue", suggestion="do it"):
    return {"section": section, "severity": severity, "issue": issue, "suggestion": suggestion}


def accept(i, ok=True, reason="r"):
    return {"index": i, "accept": ok, "reason": reason}


# --------------------------------------------------------------------------- #
# (a) Single-iteration mechanics                                              #
# --------------------------------------------------------------------------- #

def test_single_iteration_valid_iteration_score(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {
        "profile": ("profile", False, "alpha only"),
        "interests": ("interests", True, "cycling"),
    })
    oai = fake_openai(crit(7.0, {"profile": 7.5}, [item("profile", "major")]))
    cla = fake_claude(decision_sets=[[accept(0)]], revision_texts=["alpha beta revised profile"])

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
                 max_iterations=1, claude_client=cla, openai_client=oai)

    assert isinstance(res, RefinementResult) and len(res.iterations) == 1
    it = res.iterations[0]
    assert isinstance(it, IterationScore) and it.iteration == 1
    # static section excluded from scoring (D-13); profile present and typed
    assert set(it.section_scores) == {"profile"}
    ps = it.section_scores["profile"]
    assert isinstance(ps, SectionScore) and ps.critique_score == 7.5 and ps.current_version == 1
    # accepted major item → section revised to v1 on disk + manifest advanced
    assert manifest["profile"]["version"] == 1
    assert "beta" in ctx.read_section("profile", version=1)
    # one major item present → not frozen → stops at the hard cap
    assert res.convergence_reason == "max_iterations" and res.converged is False
    assert ps.converged is False


def test_rejected_item_not_applied_and_no_revision(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {"profile": ("profile", False, "alpha only")})
    oai = fake_openai(crit(6.0, {"profile": 6.0}, [item("profile", "major")]))
    cla = fake_claude(decision_sets=[[accept(0, ok=False, reason="would drop a metric the JD wants")]])

    refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
           max_iterations=1, claude_client=cla, openai_client=oai)

    # rejected → version unchanged, no v1 written
    assert manifest["profile"]["version"] == 0
    assert not ctx.section_path("profile", version=1).exists()


def test_iteration_checkpoint_and_audit_written(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {"profile": ("profile", False, "alpha only")})
    oai = fake_openai(crit(7.0, {"profile": 7.5}, [item("profile", "major")]))
    cla = fake_claude(decision_sets=[[accept(0)]], revision_texts=["alpha beta x"])

    refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
           max_iterations=1, claude_client=cla, openai_client=oai)

    assert (ctx.output_dir / "iteration_1.json").exists()
    assert (ctx.output_dir / "critique_iter_1.json").exists()
    assert (ctx.output_dir / "run_log.jsonl").exists()


# --------------------------------------------------------------------------- #
# (b) Freezing + multi-iteration convergence                                  #
# --------------------------------------------------------------------------- #

def test_frozen_section_excluded_from_iteration_2_critique(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {
        "profile": ("profile", False, "alpha only"),   # only minor in iter1 → freezes
        "skills": ("skills", False, "alpha only"),      # major in iter1 → stays active
    })
    oai = fake_openai(
        crit(7.0, {"profile": 9.0, "skills": 5.0}, [item("profile", "minor"), item("skills", "major")]),
        crit(8.0, {"skills": 8.0}, [item("skills", "minor")]),   # iter2: only skills
    )
    cla = fake_claude(
        decision_sets=[[accept(0), accept(1)], [accept(0, ok=False)]],
        revision_texts=["alpha beta profile polished", "alpha beta skills improved"],
    )

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
                 max_iterations=2, claude_client=cla, openai_client=oai)

    # iter1 saw both; iter2 saw ONLY skills (profile frozen) — the load-bearing check
    assert oai.state["seen"][0] == {"profile", "skills"}
    assert oai.state["seen"][1] == {"skills"}
    # profile converged in iter1, skills in iter2 → all frozen
    assert res.iterations[0].section_scores["profile"].converged is True
    assert res.iterations[0].sections_converged == 1
    assert res.converged is True and res.convergence_reason == "all_sections_converged"


def test_dual_signal_convergence(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {"profile": ("profile", False, "alpha lonely")})  # coverage 0.5 at v0
    # both iters keep a major item (never freezes), tiny score + coverage deltas
    oai = fake_openai(
        crit(7.0, {"profile": 7.0}, [item("profile", "major")]),
        crit(7.0, {"profile": 7.2}, [item("profile", "major")]),
    )
    cla = fake_claude(
        decision_sets=[[accept(0)], [accept(0)]],
        revision_texts=["alpha beta one", "alpha beta two"],   # both coverage 1.0 → Δkw=0 at iter2
    )

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
                 max_iterations=3, claude_client=cla, openai_client=oai)

    assert len(res.iterations) == 2
    assert res.converged is True and res.convergence_reason == "dual_signal_converged"
    assert abs(res.iterations[1].keyword_delta) < 0.05
    assert abs(res.iterations[1].critique_delta) < 0.5
    assert manifest["profile"]["version"] == 2


def test_soft_stop_zero_major(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {"profile": ("profile", False, "alpha beta done")})
    # zero major anywhere → both freezing AND soft-stop apply; all-frozen wins the label
    oai = fake_openai(crit(9.0, {"profile": 9.0}, [item("profile", "minor")]))
    cla = fake_claude(decision_sets=[[accept(0, ok=False)]])   # reject the minor, nothing to revise

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
                 max_iterations=3, claude_client=cla, openai_client=oai)

    assert res.converged is True
    # single section, zero major → it freezes → all frozen
    assert res.convergence_reason == "all_sections_converged"
    assert res.iterations[0].section_scores["profile"].converged is True


def test_rubric_extended_during_loop(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    manifest = setup_sections(ctx, {"profile": ("profile", False, "alpha only")})
    oai = fake_openai(crit(7.0, {"profile": 7.0}, [item("profile", "major")], additions=["gamma"]))
    cla = fake_claude(
        decision_sets=[[accept(0)]],
        revision_texts=["alpha beta gamma"],
        rubric_decision_sets=[[{"keyword": "gamma", "implied_by_jd": True, "reason": "JD implies it"}]],
    )

    res = refine(manifest, jd(), rubric(), budgets(), ctx, model="m",
                 max_iterations=1, claude_client=cla, openai_client=oai)

    assert res.final_rubric.version == 2
    assert "gamma" in res.final_rubric.required_keywords
    assert any(a.keyword == "gamma" for a in res.final_rubric.added_from_critique)
