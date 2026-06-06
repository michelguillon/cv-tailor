"""Verifier / fabrication gate tests (F-35), mocked Anthropic — no API."""

import types

from tailor.run_context import RunContext
from tailor.tools.verifier import verify_run, verify_section


def fake_claude(*payloads):
    """Each call returns the next report_grounding payload (default: all grounded)."""
    q = list(payloads)

    def create(**kwargs):
        inp = q.pop(0) if q else {"unsupported": []}
        block = types.SimpleNamespace(type="tool_use", name="report_grounding", input=inp)
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def test_verify_section_flags_unsupported_claim():
    client = fake_claude({"unsupported": [
        {"claim": "fintech sector leadership", "kind": "sector", "reason": "source is adtech"}]})
    flags = verify_section("Led fintech teams", "Led adtech teams", model="m", client=client)
    assert len(flags) == 1 and flags[0]["kind"] == "sector" and "fintech" in flags[0]["claim"]


def test_verify_section_empty_when_grounded():
    client = fake_claude({"unsupported": []})
    assert verify_section("Led adtech teams", "Led adtech teams", model="m", client=client) == []


def test_verify_section_is_a_safety_net_on_bad_output():
    """Malformed / missing tool output must never crash the run — return no findings."""
    def create(**kwargs):
        block = types.SimpleNamespace(type="text", text="oops")
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    assert verify_section("x", "y", model="m", client=client) == []


def test_verify_run_checks_only_nonstatic_sections_with_a_source(tmp_path):
    ctx = RunContext.create(run_id="r", base_dir=tmp_path)
    ctx.write_section("profile", "Led adtech teams", source=True)     # raw ground truth
    ctx.write_section("profile", "Led fintech teams", version=1)      # tailored draft
    ctx.write_section("header", "Michel Guillon", static=True)        # static → skipped
    ctx.write_section("skills", "Python, Go", version=1)              # non-static but NO source → skipped
    manifest = {
        "profile": {"static": False, "version": 1},
        "header": {"static": True, "version": None},
        "skills": {"static": False, "version": 1},
    }
    client = fake_claude({"unsupported": [{"claim": "fintech", "kind": "sector", "reason": "adtech only"}]})
    flags = verify_run(ctx, manifest, model="m", client=client)

    assert set(flags) == {"profile"}                                 # only the one with a source
    item = flags["profile"][0]
    assert item.severity == "major" and item.source_writer == "verifier" and "fintech" in item.issue
