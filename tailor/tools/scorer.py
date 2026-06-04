"""tools/scorer.py — keyword-coverage scoring at section level (SPEC §5, Step 2).

`keyword_coverage(section_text, rubric)` is the primitive: the fraction of a
rubric's required keywords present in one section's text. The Phase 3 loop scores
each active section and derives aggregates from these (D-12). `union_coverage`
gives the CV-level "what fraction of the rubric does this CV cover anywhere",
used for fit reasoning.

Matching is deterministic (no model, no API). Keyword and text are normalised so
case, hyphens, and punctuation don't matter. A keyword counts as present when
EITHER its tokens appear as a contiguous run, OR all of its *significant* tokens
(after dropping a small stoplist) appear as whole tokens anywhere in the text.

The token-subset rule (F-10) fixes a brittleness found on real data: an
already-tailored CV scored ~0.11 against its own JD's rubric because Phase 0
emits multi-word keywords ("go-to-market strategy", "executive communication")
whose words appear in the CV reworded or non-adjacent. Exact-phrase matching
missed them, making the convergence signal near-useless. Token-subset matching
recovers the real coverage while staying deterministic — what the delta-based
convergence signal needs (R-08: a score is only useful if it discriminates
consistently).

Known limitation: no stemming ("api" ≠ "apis"); token-subset can slightly
over-credit when a keyword's words appear unrelated in a long text. Acceptable
for a coverage heuristic; revisit if real runs show false positives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tailor.models import ScoringRubric

__all__ = [
    "normalise",
    "keyword_present",
    "matched_keywords",
    "keyword_coverage",
    "union_coverage",
    "CoverageReport",
    "coverage_report",
]


# Dropped from multi-word keywords before the token-subset check — these carry
# no discriminating signal and appear in almost any CV.
_STOPWORDS = {"to", "of", "and", "the", "a", "an", "in", "for", "with", "on", "as"}


def normalise(text: str) -> str:
    """Lowercase; collapse any run of non-alphanumerics to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def keyword_present(keyword: str, normalised_text: str) -> bool:
    """True if ``keyword`` is present in ``normalised_text`` (already normalised).

    Two ways to match (F-10): the keyword's tokens appear as a contiguous run, OR
    all of its significant tokens (minus a small stoplist) appear as whole tokens
    anywhere. The first is exact; the second recovers reworded/non-adjacent
    phrasings ("go-to-market strategy" ↔ a CV that says "...go-to-market... overall
    strategy..."). Space-padding keeps matches token-aligned ("api" ≠ "rapidly").
    """
    kw_tokens = normalise(keyword).split()
    if not kw_tokens:
        return False
    if f" {' '.join(kw_tokens)} " in f" {normalised_text} ":   # contiguous fast path
        return True
    significant = [t for t in kw_tokens if t not in _STOPWORDS] or kw_tokens
    if len(significant) < 2:                                   # single-token: contiguous only
        return False
    text_tokens = set(normalised_text.split())
    return all(t in text_tokens for t in significant)


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    """The subset of ``keywords`` present in ``text`` (order preserved)."""
    norm = normalise(text)
    return [kw for kw in keywords if keyword_present(kw, norm)]


def keyword_coverage(text: str, rubric: ScoringRubric, *, keywords: list[str] | None = None) -> float:
    """Fraction of ``keywords`` (default: rubric.required_keywords) present in ``text``.

    Returns 0.0 when there are no keywords to score against (an empty rubric can't
    be "covered"); callers treat that as a degenerate case, not a perfect score.
    """
    pool = keywords if keywords is not None else rubric.required_keywords
    if not pool:
        return 0.0
    return len(matched_keywords(text, pool)) / len(pool)


def union_coverage(texts: list[str], rubric: ScoringRubric, *, keywords: list[str] | None = None) -> float:
    """Fraction of keywords present in ANY of ``texts`` (CV-level coverage)."""
    pool = keywords if keywords is not None else rubric.required_keywords
    if not pool:
        return 0.0
    combined = normalise(" \n ".join(texts))
    return sum(1 for kw in pool if keyword_present(kw, combined)) / len(pool)


@dataclass
class CoverageReport:
    coverage: float                 # required-keyword coverage (0–1)
    matched: list[str]              # required keywords found
    missing: list[str]              # required keywords absent
    nice_to_have_coverage: float    # secondary signal (0–1)
    nice_to_have_matched: list[str]


def coverage_report(text: str, rubric: ScoringRubric) -> CoverageReport:
    """Full breakdown for one section: required + nice-to-have, matched + missing.

    Useful at HITL/diagnostics — "which JD keywords is this section still missing?"
    """
    norm = normalise(text)
    req = rubric.required_keywords
    matched = [kw for kw in req if keyword_present(kw, norm)]
    missing = [kw for kw in req if kw not in matched]
    nth = rubric.nice_to_have_keywords
    nth_matched = [kw for kw in nth if keyword_present(kw, norm)]
    return CoverageReport(
        coverage=(len(matched) / len(req)) if req else 0.0,
        matched=matched,
        missing=missing,
        nice_to_have_coverage=(len(nth_matched) / len(nth)) if nth else 0.0,
        nice_to_have_matched=nth_matched,
    )
