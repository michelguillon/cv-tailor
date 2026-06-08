"""Phase 0 JD analysis tests with a mocked Mistral client (no API).

Validates the parse → schema-build → validate/retry path, not the model.
"""

import json
import types

import pytest

from tailor.models import JDAnalysis, ScoringRubric
from tailor.phases.phase0_jd_analysis import JDAnalysisError, analyse_jd

VALID = {
    "role_title": "Director, Solutions Engineering",
    "company_name": "Airwallex",
    "seniority_level": "director",
    "key_requirements": ["lead EMEA SE teams", "10+ years client-facing"],
    "nice_to_haves": ["payments ecosystem"],
    "company_context": "Airwallex is a global payments platform.",
    "tone_signals": ["technical", "high-growth"],
    "required_keywords": ["Pre-Sales", "pre-sales", "EMEA", "payments"],  # dup + case
    "nice_to_have_keywords": ["Fintech"],
    "structural_requirements": ["quantify team scale"],
}


def fake_client(*contents, tokens=(100, 50)):
    """A stand-in Mistral client whose chat.complete returns canned JSON strings."""
    responses = []
    for c in contents:
        usage = types.SimpleNamespace(
            prompt_tokens=tokens[0], completion_tokens=tokens[1], total_tokens=sum(tokens)
        )
        msg = types.SimpleNamespace(content=c)
        responses.append(types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage))

    calls = {"n": 0}

    def complete(**kwargs):
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return r

    return types.SimpleNamespace(chat=types.SimpleNamespace(complete=complete))


def test_valid_response_builds_schemas():
    client = fake_client(json.dumps(VALID))
    jd, rubric, usage = analyse_jd("raw jd text", model="m", client=client)
    assert isinstance(jd, JDAnalysis) and isinstance(rubric, ScoringRubric)
    assert jd.role_title == "Director, Solutions Engineering"
    assert jd.company_name == "Airwallex"
    assert jd.seniority_level == "director"
    assert jd.raw_text == "raw jd text"
    assert rubric.version == 1
    assert rubric.created_at and rubric.updated_at      # stamped by code
    assert rubric.added_from_critique == []
    assert usage.total_tokens == 150


def test_required_keywords_lowercased_and_deduped():
    jd, rubric, _ = analyse_jd("x", model="m", client=fake_client(json.dumps(VALID)))
    assert rubric.required_keywords == ["pre-sales", "emea", "payments"]  # dedup, order kept


def test_company_name_optional_and_cleaned():
    """Inferred company is optional (F-47): missing/placeholder → None; a real name is trimmed."""
    no_co = {k: v for k, v in VALID.items() if k != "company_name"}
    jd, *_ = analyse_jd("x", model="m", client=fake_client(json.dumps(no_co)))
    assert jd.company_name is None                       # absent → None (no validation failure)
    junk = json.dumps({**VALID, "company_name": "N/A"})
    assert analyse_jd("x", model="m", client=fake_client(junk))[0].company_name is None
    real = json.dumps({**VALID, "company_name": "  Globex  "})
    assert analyse_jd("x", model="m", client=fake_client(real))[0].company_name == "Globex"


def test_retries_once_on_invalid_then_succeeds():
    bad = json.dumps({**VALID, "seniority_level": "wizard"})   # not in KNOWN_SENIORITY
    client = fake_client(bad, json.dumps(VALID))
    jd, rubric, _ = analyse_jd("x", model="m", client=client)
    assert jd.seniority_level == "director"


def test_raises_after_persistent_invalid():
    bad = json.dumps({**VALID, "required_keywords": []})        # empty → nothing to score
    with pytest.raises(JDAnalysisError):
        analyse_jd("x", model="m", client=fake_client(bad, bad))


def test_raises_on_non_json():
    with pytest.raises(JDAnalysisError):
        analyse_jd("x", model="m", client=fake_client("not json", "still not json"))
