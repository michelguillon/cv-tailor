"""Step 8: RunConfig resolution / mode gating + HITL handlers (no API).

Full mocked end-to-end run is Step 9 (test_phases.py); Step 8 is verified by a
live demo run.
"""

import types

import pytest

from tailor.config import ConfigError, load_config, resolve_run_config
from tailor.models import FitAssessment
from tailor.run import AutoHITL, TerminalHITL


def cfg():
    return load_config()    # the real config.yaml (stable) from /app


# -- mode gating (§3.7, D-08) ----------------------------------------------- #

def test_demo_resolves_haiku_one_iteration():
    rc = resolve_run_config(cfg(), mode="demo")
    assert rc.orchestrator_model == "claude-haiku-4-5" and rc.max_iterations == 1
    assert rc.gpt_model == "gpt-4o-mini" and rc.validation_model == "claude-haiku-4-5"


def test_full_requires_key(monkeypatch):
    monkeypatch.delenv("FULL_MODE_KEY", raising=False)
    with pytest.raises(ConfigError):
        resolve_run_config(cfg(), mode="full")


def test_full_unlocks_with_correct_key(monkeypatch):
    monkeypatch.setenv("FULL_MODE_KEY", "s3cret")
    rc = resolve_run_config(cfg(), mode="full", key="s3cret")
    assert rc.orchestrator_model == "claude-sonnet-4-6" and rc.max_iterations == 3


def test_full_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("FULL_MODE_KEY", "s3cret")
    with pytest.raises(ConfigError):
        resolve_run_config(cfg(), mode="full", key="nope")


def test_unknown_mode_raises():
    with pytest.raises(ConfigError):
        resolve_run_config(cfg(), mode="turbo")


def test_max_iterations_override():
    assert resolve_run_config(cfg(), mode="demo", max_iterations=5).max_iterations == 5


# -- HITL handlers ----------------------------------------------------------- #

def test_autohitl_proceeds_and_stops_on_no_fit():
    h = AutoHITL()
    assert h.fit(types.SimpleNamespace(outcome="partial"), None) is True
    assert h.fit(types.SimpleNamespace(outcome="no_fit"), None) is False
    assert AutoHITL(override_no_fit=True).fit(types.SimpleNamespace(outcome="no_fit"), None) is True
    assert h.formatting({"profile": {}}, {}) is True
    assert h.review(None, None, None, None, None, None) is None


def test_terminal_fit_stop_and_proceed():
    fit = FitAssessment(outcome="partial", overall_fit_score=0.5, recommended_sections={})
    jd = types.SimpleNamespace(role_title="Director, SE")
    assert TerminalHITL(input_fn=lambda _: "s", print_fn=lambda *_: None).fit(fit, jd) is False
    assert TerminalHITL(input_fn=lambda _: "p", print_fn=lambda *_: None).fit(fit, jd) is True


def test_terminal_no_fit_requires_override():
    fit = FitAssessment(outcome="no_fit", overall_fit_score=0.2, no_fit_reason="needs clearance")
    jd = types.SimpleNamespace(role_title="X")
    assert TerminalHITL(input_fn=lambda _: "s", print_fn=lambda *_: None).fit(fit, jd) is False
    assert TerminalHITL(input_fn=lambda _: "o", print_fn=lambda *_: None).fit(fit, jd) is True
