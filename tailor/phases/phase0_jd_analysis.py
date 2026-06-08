"""Phase 0 — JD analysis (Mistral). SPEC §5 Phase 0.

Input:  raw JD text.
Output: a validated `JDAnalysis` + the initial `ScoringRubric` (v1).
Model:  Mistral (forced JSON output via response_format), model id from config.

The provider is hidden behind this phase (D-02): callers get typed objects. The
structured output is validated against the schema before it is returned (R-09) —
a malformed extraction is retried once, then surfaced, never passed downstream.

Two distinct outputs from one call:
  - JDAnalysis.key_requirements  = the JD's stated requirements, as phrases.
  - ScoringRubric.required_keywords = short atomic terms the CV is *scored* against
    (skills/tools/domains), which the scorer matches per section.
"""

from __future__ import annotations

import json

from tailor.audit import utc_now_iso
from tailor.helpers import call_with_retry, get_mistral_client
from tailor.models import JDAnalysis, ScoringRubric

__all__ = ["analyse_jd", "JDAnalysisError"]

# Seniority labels must line up with the corpus vocabulary (corpus.metadata).
KNOWN_SENIORITY = {"senior", "principal", "director", "vp", "executive"}

_SYSTEM_PROMPT = """\
You are a precise job-description analyst. Extract a STRUCTURED analysis of the \
JD for a CV-tailoring system. Return ONLY a single JSON object, no prose, with \
exactly these keys:

{
  "role_title": string,                // the role as titled, e.g. "Director, Solutions Engineering"
  "company_name": string or null,      // the hiring company's NAME only, e.g. "Airwallex" (NOT a
                                       //   description); null if the JD does not name the employer
  "seniority_level": string,           // one of: senior | principal | director | vp | executive
  "key_requirements": [string],        // the JD's stated requirements, as short phrases (5-12 items)
  "nice_to_haves": [string],           // preferred/advantageous, not mandatory (0-8 items)
  "company_context": string,           // 1-2 sentences: company, product, scale, market
  "tone_signals": [string],            // e.g. "technical", "startup", "high-growth", "formal"
  "required_keywords": [string],       // ATOMIC terms to score a CV against: skills, tools,
                                       //   domains, methods (e.g. "pre-sales", "EMEA",
                                       //   "API", "payments", "team leadership"). 8-20 items,
                                       //   lowercase, no duplicates, each 1-3 words.
  "nice_to_have_keywords": [string],   // atomic terms that strengthen but aren't required
  "structural_requirements": [string]  // CV-structure asks, e.g. "quantify achievements",
                                       //   "show team scale", "lead with leadership impact"
}

Rules:
- CLASSIFICATION: respect the JD's own markers. Anything the JD calls \
"preferred", "advantageous", "a plus", "nice to have", or "not required" goes in \
nice_to_haves / nice_to_have_keywords. Stated responsibilities and \
"required"/"must have"/"who you are" items go in key_requirements / \
required_keywords. Do NOT demote a core responsibility to a nice-to-have.
- required_keywords are for keyword-coverage scoring: keep them atomic, \
de-duplicated, and DISCRIMINATING. Exclude generic standalone words that almost \
any CV would match (e.g. "product", "engineering", "software", "technical", \
"leadership", "ai" on its own) — prefer specific multi-word terms ("ai delivery", \
"cloud-native architecture", "solutions engineering").
- structural_requirements must be SPECIFIC to this JD (reference its concrete \
asks, e.g. team size, named metrics, domains), not generic CV advice.
- Infer seniority_level from scope and stated years, mapping to the closest label.
- company_name is the employer's name ONLY (not a tagline or description); use null when \
the JD doesn't state it — do NOT guess from the product, recruiter, or a generic phrase.
- Do not invent requirements not supported by the JD text.
"""

# Junk values an LLM sometimes emits for an unknown company — treated as "no name".
_COMPANY_BLANKS = {"null", "none", "n/a", "na", "unknown", "not stated", "not specified",
                   "the company", "company", "confidential"}


class JDAnalysisError(RuntimeError):
    """Raised when the JD extraction can't be validated after a retry (R-09)."""


def _validate(data: dict) -> list[str]:
    """Return a list of validation problems (empty = valid)."""
    problems: list[str] = []
    required_str = ["role_title", "seniority_level", "company_context"]
    required_list = [
        "key_requirements", "nice_to_haves", "tone_signals",
        "required_keywords", "nice_to_have_keywords", "structural_requirements",
    ]
    for k in required_str:
        if not isinstance(data.get(k), str) or not data[k].strip():
            problems.append(f"{k} must be a non-empty string")
    for k in required_list:
        if not isinstance(data.get(k), list):
            problems.append(f"{k} must be a list")
    if not data.get("required_keywords"):
        problems.append("required_keywords must be non-empty (nothing to score against)")
    sl = str(data.get("seniority_level", "")).lower()
    if sl not in KNOWN_SENIORITY:
        problems.append(f"seniority_level {data.get('seniority_level')!r} not in {sorted(KNOWN_SENIORITY)}")
    cn = data.get("company_name")           # optional: a string or null/absent
    if cn is not None and not isinstance(cn, str):
        problems.append("company_name must be a string or null")
    return problems


def _clean_company(value) -> str | None:
    """Normalise the extracted company name; junk/blank/placeholder → None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return None if (not v or v.lower() in _COMPANY_BLANKS) else v


def _call(client, model, jd_text, seed):
    resp = call_with_retry(
        client.chat.complete,
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": jd_text},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        random_seed=seed,
    )
    return resp


def _norm_keywords(items: list[str]) -> list[str]:
    """Lowercase, strip, de-duplicate (preserve order) — keywords are for matching."""
    seen, out = set(), []
    for it in items:
        k = str(it).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def analyse_jd(jd_text: str, *, model: str, client=None, seed: int = 7):
    """Extract (JDAnalysis, ScoringRubric_v1, usage) from raw JD text.

    `usage` is the raw Mistral usage object (prompt/completion/total tokens) so
    callers can track cost. Validates the extraction; retries once on failure.
    """
    client = client or get_mistral_client()

    last_problems: list[str] = []
    resp = None
    for attempt in range(2):
        resp = _call(client, model, jd_text, seed + attempt)
        try:
            data = json.loads(resp.choices[0].message.content)
        except json.JSONDecodeError as exc:
            last_problems = [f"response was not valid JSON: {exc}"]
            continue
        last_problems = _validate(data)
        if not last_problems:
            break
    if last_problems:
        raise JDAnalysisError(
            f"JD analysis failed validation after retry ({model}): " + "; ".join(last_problems)
        )

    now = utc_now_iso()
    jd = JDAnalysis(
        raw_text=jd_text,
        role_title=data["role_title"].strip(),
        seniority_level=data["seniority_level"].strip().lower(),
        key_requirements=[s.strip() for s in data["key_requirements"]],
        nice_to_haves=[s.strip() for s in data["nice_to_haves"]],
        company_context=data["company_context"].strip(),
        tone_signals=[s.strip() for s in data["tone_signals"]],
        company_name=_clean_company(data.get("company_name")),
    )
    rubric = ScoringRubric(
        version=1,
        required_keywords=_norm_keywords(data["required_keywords"]),
        nice_to_have_keywords=_norm_keywords(data["nice_to_have_keywords"]),
        structural_requirements=[s.strip() for s in data["structural_requirements"]],
        created_at=now,
        updated_at=now,
        added_from_critique=[],
    )
    return jd, rubric, getattr(resp, "usage", None)
