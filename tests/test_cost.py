"""Cost-tracking tests (D-08, F-08) — deterministic, no API."""

import types

from tailor import cost
from tailor.helpers import claude_complete, gpt_complete


def test_breakdown_and_total():
    t = cost.CostTracker()
    t.note("claude-haiku-4-5", 1_000_000, 1_000_000)   # $1 in + $5 out
    t.note("gpt-4o-mini", 2_000_000, 0)                # $0.30 in
    bd = t.breakdown()
    assert bd["anthropic_haiku"] == 6.0
    assert bd["openai_gpt4o_mini"] == 0.30
    assert t.total_usd() == 6.30


def test_model_key_collapses_mistral_models():
    t = cost.CostTracker()
    t.note("mistral-small-latest", 1_000_000, 0)
    t.note("mistral-embed", 1_000_000, 0)
    assert set(t.breakdown()) == {"mistral_small"}    # both map to one key (D-08)


def test_track_context_records_then_noops_outside():
    with cost.track() as t:
        cost.note("claude-haiku-4-5", 100, 50)
        assert cost.active() is t
    assert t.tokens["claude-haiku-4-5"] == [100, 50]
    assert cost.active() is None
    cost.note("claude-haiku-4-5", 1, 1)               # no active tracker → no-op, no error


def test_footer_is_labelled_estimate():
    t = cost.CostTracker()
    t.note("gpt-4o-mini", 1_000_000, 0)
    f = t.footer(mode="demo", iterations_run=2)
    assert f["type"] == "run_complete" and f["mode"] == "demo" and f["iterations_run"] == 2
    assert "estimate" in f["note"] and f["total_estimated_usd"] == 0.15
    assert f["total_estimated_gbp"] == round(0.15 * 0.79, 6)


# -- helpers → cost wiring (the central capture point) ---------------------- #

def _fake_anthropic(input_tokens, output_tokens, cache_read=0):
    def create(**kwargs):
        u = types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                                  cache_read_input_tokens=cache_read, cache_creation_input_tokens=0)
        return types.SimpleNamespace(content=[], usage=u)
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def _fake_openai(prompt_tokens, completion_tokens):
    def create(**kwargs):
        u = types.SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        return types.SimpleNamespace(choices=[], usage=u)
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def test_claude_complete_records_usage_including_cache():
    with cost.track() as t:
        claude_complete(model="claude-haiku-4-5", messages=[{"role": "user", "content": "x"}],
                        client=_fake_anthropic(100, 20, cache_read=80))
    assert t.tokens["claude-haiku-4-5"] == [180, 20]   # cache read folded into input (F-22)


def test_gpt_complete_records_usage():
    with cost.track() as t:
        gpt_complete(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}],
                     client=_fake_openai(300, 40))
    assert t.tokens["gpt-4o-mini"] == [300, 40]
