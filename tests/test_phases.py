"""Step 9 — fully-mocked end-to-end pipeline test (Phase 0 → 6), no API.

The deferred E2E from Step 8: one `run_pipeline` pass with all three SDK providers
faked (Mistral JD analysis, Anthropic claude/orchestrator/haiku, OpenAI gpt writer)
and `AutoHITL`. Verifies the deterministic scaffold around the agentic loop end to
end: outputs written, run_log complete, cost footer accurate, freeze deterministic,
and `replay` reads it all back.

Providers are injected by monkeypatching the client getters at their resolution
points — `phase0`'s imported `get_mistral_client`, and `helpers.get_{anthropic,
openai}_client` (which `claude_complete`/`gpt_complete` resolve at call time). The
corpus is a fixture (no ChromaDB) via `run.all_sections`. Fakes are prompt-aware:
they read `SECTION TYPE:` / `tool_choice` to serve every phase from one client.
"""

import json
import re
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from tailor import helpers
from tailor import run as run_mod
from tailor.__main__ import cli
from tailor.audit import read_entries
from tailor.cost import PRICES_USD_PER_MTOK
from tailor.phases import phase0_jd_analysis
from tailor.run import AutoHITL, run_pipeline

# Fixed per-call usage so the footer is computable from call counts (cost test).
A_IN, A_OUT = 1000, 100      # every Anthropic call (all Haiku in demo)
O_IN, O_OUT = 500, 50        # every OpenAI call (gpt-4o-mini)
M_IN, M_OUT = 200, 20        # the single Mistral call (phase 0)

JD_JSON = json.dumps({
    "role_title": "Director, Solutions Engineering",
    "seniority_level": "director",
    "key_requirements": ["lead EMEA teams", "alpha delivery at scale"],
    "nice_to_haves": ["beta exposure"],
    "company_context": "A global payments platform.",
    "tone_signals": ["technical", "high-growth"],
    "required_keywords": ["alpha", "beta", "gamma"],
    "nice_to_have_keywords": ["delta"],
    "structural_requirements": ["quantify team scale"],
})


def fixture_sections() -> list[dict]:
    """A one-CV corpus whose section docs carry the rubric keywords (alpha/beta/
    gamma), so coverage is real. header + interests are static (D-13/D-20)."""
    f = "CV_Michel_Guillon_2026_ai.docx"

    def s(sid, stype, doc, *, static=False, company=None, title="", position=0):
        return {"section_id": sid, "section_type": stype, "filename": f,
                "cv_type": "job_specific", "seniority": "director",
                "title": title, "company": company, "position": position,
                "version_date": "2026-01", "static": static, "document": doc}

    return [
        s("header", "header", "Michel Guillon — London", static=True, position=0),
        s("profile", "profile", "Director with alpha, beta and gamma experience.", position=1),
        s("skills", "skills", "alpha, beta, gamma, team leadership.", position=2),
        s("experience_acme", "experience", "Led alpha beta gamma delivery at Acme.",
          company="Acme", title="Engineering Lead", position=3),
        s("interests", "interests", "Cycling and chess.", static=True, position=8),
    ]


def _prompt(messages) -> str:
    return " ".join(m["content"] for m in messages if isinstance(m.get("content"), str))


def make_fakes(rec: dict):
    """Build the three fake SDK clients. `rec` accumulates per-provider call counts
    so the cost footer can be checked against known token usage."""

    def mistral_complete(**kwargs):                       # phase 0 JD analysis
        rec["mistral"] += 1
        msg = types.SimpleNamespace(content=JD_JSON)
        usage = types.SimpleNamespace(prompt_tokens=M_IN, completion_tokens=M_OUT,
                                      total_tokens=M_IN + M_OUT)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mistral_client = types.SimpleNamespace(chat=types.SimpleNamespace(complete=mistral_complete))

    def anthropic_create(**kwargs):
        rec["anthropic"] += 1
        usage = types.SimpleNamespace(input_tokens=A_IN, output_tokens=A_OUT,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0)
        tc = kwargs.get("tool_choice")
        prompt = _prompt(kwargs.get("messages", []))
        if tc is None:                                   # phase 2 draft — plain text
            m = re.search(r"SECTION TYPE: (\S+)", prompt)
            sid = m.group(1) if m else "section"
            block = types.SimpleNamespace(type="text", text=f"{sid} alpha beta gamma")
            return types.SimpleNamespace(content=[block], usage=usage)

        name = tc["name"]
        if name == "submit_fit_assessment":
            inp = {"outcome": "strong", "skills_transferable": ["delivery at scale"],
                   "gaps": [], "no_fit_reason": None,
                   "value_alignment_notes": "scaling technical orgs maps to this role"}
        elif name == "submit_draft":                     # claude_writer
            sid = re.search(r"SECTION TYPE: (\S+)", prompt).group(1)
            inp = {"text": f"{sid} alpha beta gamma", "items": []}
        elif name == "submit_decision":                  # orchestrator
            inp = {"selected_base": "claude", "final_text": "", "direction": "tighten the opening",
                   "synthesis_notes": None, "claude_quality": 8.0, "gpt_quality": 7.0,
                   "converged": True, "rubric_additions": []}
        elif name == "submit_pushback":
            inp = {"disagree": False, "reasoning": ""}
        elif name == "revise_direction":
            inp = {"direction": "hold", "revised": False, "reasoning": "ok"}
        elif name == "submit_rubric_decisions":
            inp = {"decisions": []}
        elif name == "report_grounding":                 # verifier — everything grounded
            inp = {"unsupported": []}
        elif name == "submit_formatting":                # phase 5 — no corrections
            inp = {"corrected_text": "unchanged", "corrections": []}
        elif name == "interpret_revision":               # phase 4 free-text (unused by AutoHITL)
            inp = {"section_id": "profile", "instruction": "tighten"}
        else:
            inp = {}
        block = types.SimpleNamespace(type="tool_use", name=name, input=inp)
        return types.SimpleNamespace(content=[block], usage=usage)

    anthropic_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=anthropic_create))

    def openai_create(**kwargs):
        rec["openai"] += 1
        usage = types.SimpleNamespace(prompt_tokens=O_IN, completion_tokens=O_OUT)
        fmt = kwargs["response_format"]["json_schema"]["name"]
        prompt = _prompt(kwargs.get("messages", []))
        if fmt == "writer_draft":
            sid = re.search(r"SECTION TYPE: (\S+)", prompt).group(1)
            content = json.dumps({"text": f"{sid} alpha beta gamma", "items": []})
        else:                                            # pushback
            content = json.dumps({"disagree": False, "reasoning": ""})
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    openai_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=openai_create)))

    return mistral_client, anthropic_client, openai_client


@pytest.fixture
def run_demo(monkeypatch, tmp_path):
    """Wire the fakes and return a runner: `go(**kw) -> (summary, rec)`. Uses the
    real config.yaml / budgets.yaml / templates (all git-tracked, stable)."""
    rec = {"anthropic": 0, "openai": 0, "mistral": 0}
    mclient, aclient, oclient = make_fakes(rec)
    monkeypatch.setattr(run_mod, "all_sections", lambda *a, **k: fixture_sections())
    monkeypatch.setattr(phase0_jd_analysis, "get_mistral_client", lambda *a, **k: mclient)
    monkeypatch.setattr(helpers, "get_anthropic_client", lambda *a, **k: aclient)
    monkeypatch.setattr(helpers, "get_openai_client", lambda *a, **k: oclient)

    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Tailor my CV for this role.", encoding="utf-8")
    out_base = tmp_path / "out"

    def go(**kw):
        summary = run_pipeline(str(jd_path), mode="demo", output_dir=str(out_base),
                               hitl=AutoHITL(), **kw)
        return summary, rec

    return go


def _footer(out_dir: Path) -> dict:
    entries = read_entries(out_dir / "run_log.jsonl")
    return next(e for e in entries if e.get("type") == "run_complete")


# --------------------------------------------------------------------------- #
# (1) full end-to-end pass                                                    #
# --------------------------------------------------------------------------- #

def test_end_to_end_produces_outputs_and_complete_log(run_demo):
    summary, rec = run_demo(run_id="e2e")
    out = Path(summary["output_dir"])

    # all four model roles exercised in one run (mistral / claude+haiku / gpt)
    assert rec["mistral"] == 1 and rec["anthropic"] > 0 and rec["openai"] > 0

    # submittable artefacts written
    assert (out / "cv_final.md").exists() and (out / "cv_final.html").exists()
    md = (out / "cv_final.md").read_text(encoding="utf-8")
    assert "alpha" in md and "## " in md            # tailored content + assembled headings
    assert (out / "cv_final.html").read_text(encoding="utf-8").lstrip().startswith("<!")

    # summary reflects a clean single-iteration demo run
    assert summary["outcome"] == "strong"
    assert summary["iterations"] == 1 and summary["converged"] is True

    # run_log.jsonl complete: the four spine events + a footer
    entries = read_entries(out / "run_log.jsonl")
    events = {e.get("event") for e in entries}
    assert {"jd_analysed", "loop_start", "iteration_scored", "output_written"} <= events
    assert entries[-1].get("type") == "run_complete"


def test_cvcm_when_present_flows_to_fit_and_runs_clean(run_demo, monkeypatch):
    """With a value model loaded, value_alignment_notes is populated and the run completes
    normally; without one (the default), it stays None — the pipeline is unchanged (§3.9)."""
    monkeypatch.setattr(run_mod, "load_cvcm", lambda *a, **k: "I build technical orgs from zero.")
    summary, _ = run_demo(run_id="cvcm")
    fit = json.loads((Path(summary["output_dir"]) / "phase1_fit_assessment.json").read_text("utf-8"))
    assert fit["value_alignment_notes"] == "scaling technical orgs maps to this role"
    assert summary["outcome"] == "strong"               # CVCM never changes the outcome


def test_no_cvcm_leaves_value_alignment_none(run_demo, monkeypatch):
    monkeypatch.setattr(run_mod, "load_cvcm", lambda *a, **k: None)   # deterministic: no value model
    summary, _ = run_demo(run_id="nocvcm")
    fit = json.loads((Path(summary["output_dir"]) / "phase1_fit_assessment.json").read_text("utf-8"))
    assert fit["value_alignment_notes"] is None


def test_verification_gate_surfaces_flags_in_summary_and_report(run_demo, monkeypatch):
    """A fabrication flag from the verifier reaches the run summary, the report's
    Grounding tab, and the audit log — never ships silently (F-35)."""
    from tailor.tools import verifier as vmod
    # Flag the profile only (the fake drafts are "<sid> alpha beta gamma", so the sid
    # is in the draft text); every other section stays grounded.
    monkeypatch.setattr(vmod, "verify_section",
                        lambda draft, source, **k: (
                            [{"claim": "fintech leadership", "kind": "sector", "reason": "source is adtech"}]
                            if "profile" in draft else []))
    summary, _ = run_demo(run_id="gate")
    out = Path(summary["output_dir"])

    assert summary["fabrication_flags"] >= 1
    html = (out / "cv_final.html").read_text(encoding="utf-8")
    assert "fintech leadership" in html and "Grounding" in html
    events = {e.get("event") for e in read_entries(out / "run_log.jsonl")}
    assert "unsupported_claim" in events and "flags_raised" in events
    # the raw source the verifier grounds against is persisted
    assert (out / "sections" / "profile_source.md").exists()


def test_checkpoints_written_for_every_phase(run_demo):
    summary, _ = run_demo(run_id="ckpt")
    out = Path(summary["output_dir"])
    for name in ("phase0_jd_analysis", "phase0_rubric", "phase1_fit_assessment",
                 "phase2_draft_manifest", "iteration_1"):
        assert (out / f"{name}.json").exists(), name
    # section files on disk: drafted v0/v1 + static copies + per-writer drafts
    sec = out / "sections"
    assert (sec / "profile_v0.md").exists() and (sec / "profile_v1.md").exists()
    assert (sec / "header_static.md").exists()
    assert (sec / "profile_claude_v1.md").exists() and (sec / "profile_gpt_v1.md").exists()


# --------------------------------------------------------------------------- #
# (2) freeze determinism (§8)                                                 #
# --------------------------------------------------------------------------- #

def test_freeze_is_deterministic(run_demo):
    def frozen(summary):
        it = json.loads((Path(summary["output_dir"]) / "iteration_1.json").read_text("utf-8"))
        flags = {sid: sc["converged"] for sid, sc in it["section_scores"].items()}
        return flags, it["sections_converged"], it["sections_active"]

    a, _ = run_demo(run_id="det_a")
    b, _ = run_demo(run_id="det_b")
    assert frozen(a) == frozen(b)                   # same input → same freeze decision
    flags, converged, active = frozen(a)
    assert all(flags.values()) and converged == 3 and active == 0   # all 3 active froze


# --------------------------------------------------------------------------- #
# (3) cost-tracking accuracy (§8 / §9)                                        #
# --------------------------------------------------------------------------- #

def test_cost_footer_matches_known_token_counts(run_demo):
    summary, rec = run_demo(run_id="cost")
    footer = _footer(Path(summary["output_dir"]))

    # implemented §9 footer shape (the keys this test and the docs assert on)
    assert set(footer) >= {"type", "cost_breakdown_estimated_usd", "total_estimated_usd",
                           "total_estimated_gbp", "mode", "iterations_run", "note"}
    assert footer["mode"] == "demo" and footer["iterations_run"] == 1

    hin, hout = PRICES_USD_PER_MTOK["claude-haiku-4-5"]
    gin, gout = PRICES_USD_PER_MTOK["gpt-4o-mini"]
    min_, mout = PRICES_USD_PER_MTOK["mistral-small-latest"]
    exp_anth = round(rec["anthropic"] * A_IN / 1e6 * hin + rec["anthropic"] * A_OUT / 1e6 * hout, 6)
    exp_oai = round(rec["openai"] * O_IN / 1e6 * gin + rec["openai"] * O_OUT / 1e6 * gout, 6)
    exp_mistral = round(rec["mistral"] * M_IN / 1e6 * min_ + rec["mistral"] * M_OUT / 1e6 * mout, 6)

    bd = footer["cost_breakdown_estimated_usd"]
    assert bd["anthropic_haiku"] == exp_anth
    assert bd["openai_gpt4o_mini"] == exp_oai
    assert bd["mistral_small"] == exp_mistral
    assert footer["total_estimated_usd"] == round(exp_anth + exp_oai + exp_mistral, 6)
    assert footer["total_estimated_gbp"] == round(footer["total_estimated_usd"] * 0.79, 6)
    # summary mirrors the footer (the CLI prints from the summary)
    assert summary["cost_estimated_usd"] == footer["total_estimated_usd"]
    assert summary["cost_breakdown"] == bd


# --------------------------------------------------------------------------- #
# (4) replay reads checkpoints + run_log (§8)                                 #
# --------------------------------------------------------------------------- #

def test_replay_reads_checkpoints_and_log(run_demo):
    summary, _ = run_demo(run_id="rp")
    out_dir = Path(summary["output_dir"])           # <base>/rp
    res = CliRunner().invoke(cli, ["replay", "rp", "--output-dir", str(out_dir.parent),
                                   "--reasoning"])
    assert res.exit_code == 0, res.output
    assert "Director, Solutions Engineering" in res.output   # from phase0 checkpoint
    assert "strong" in res.output                            # from phase1 checkpoint
    assert "iter 1" in res.output                            # from iteration_1.json
    assert "estimated" in res.output                         # from run_complete footer
    assert "refinement" in res.output                        # reasoning trace included


def test_replay_unknown_run_errors():
    res = CliRunner().invoke(cli, ["replay", "does_not_exist", "--output-dir", "outputs"])
    assert res.exit_code != 0
