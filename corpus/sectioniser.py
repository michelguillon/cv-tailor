"""corpus/sectioniser.py — group loaded Paragraphs into canonical CV sections.

The discovery half of the "discover structure, then persist it" pattern (R-10).
Grounded in the observed corpus fingerprint (LEARNING_NOTES F-04/F-05), not in
assumed heading styles (D-15 corrected):

  - A **section header** is a paragraph whose normalised text matches a canonical
    alias (config `section_aliases`) AND that is visually elevated (size > body,
    Heading-styled, or bold) and not a bullet. Title-matching is robust where
    style/size are inconsistent across the corpus (D-19).
  - The **header** section is synthesised from everything above the first matched
    section header (name + contact lines) — it has no title to match (D-20).
  - Inside the **experience** block only, sections split per company AND per
    role-group (D-21, revised per user): companies are the largest non-bullet
    size (14pt); within a company, a new section starts at a role line that
    follows a bullet. Stacked promotions that share bullets (consecutive role
    lines with no bullet between) stay together. section_id =
    experience_<company>_<first-role-slug>.

Output is a list of ``ExtractedSection`` (the typed ``CVSection`` schema plus the
section's rendered text), ordered by appearance. Word/line counts are measured
here so they live on the model from ingestion (R-02).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from tailor.models import CVSection

from .docx_loader import Paragraph

__all__ = ["ExtractedSection", "sectionise", "detect_headers", "MIN_SECTIONS"]

# Below this, a 2-page CV almost certainly failed to parse — block ingestion (R-01).
MIN_SECTIONS = 4

# Approximate words per rendered line at body size — for CVSection.line_count
# (SPEC §3.8: "word_count / avg_words_per_line").
_WORDS_PER_LINE = 12


@dataclass
class ExtractedSection:
    """A discovered section: its CVSection metadata plus its rendered text."""
    section: CVSection
    text: str             # section body as markdown-ish plain text (no title line)
    title: str            # the header text as it appeared, e.g. "Core Skills" / "Utiq"


def _normalise(text: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace — for matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"[^\w\s&]", " ", text)      # keep & (e.g. "technical & ai projects")
    return re.sub(r"\s+", " ", text).strip()


def _slug(text: str) -> str:
    """Company name → stable section_id suffix, e.g. 'Appnexus / Xandr' → 'appnexus_xandr'."""
    norm = _normalise(text).replace("&", " and ")
    return re.sub(r"\s+", "_", norm).strip("_")


def _word_count(text: str) -> int:
    return len(text.split())


def _line_count(word_count: int) -> int:
    return max(1, math.ceil(word_count / _WORDS_PER_LINE)) if word_count else 0


def _body_size(paras: list[Paragraph]) -> float | None:
    """Most common rendered size among non-bullet paragraphs = body text size."""
    counts: dict[float, int] = {}
    for p in paras:
        if p.has_num_pr or p.rendered_size is None:
            continue
        counts[p.rendered_size] = counts.get(p.rendered_size, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _build_alias_lookup(section_aliases: dict[str, list[str]]) -> dict[str, str]:
    """Map a normalised title → canonical section_type."""
    lookup: dict[str, str] = {}
    for section_type, aliases in section_aliases.items():
        for alias in aliases or []:
            lookup[_normalise(alias)] = section_type
    return lookup


def _para_text(p: Paragraph) -> str:
    """Render one paragraph as a content line: bullets prefixed, dates appended."""
    line = f"- {p.text}" if p.has_num_pr else p.text
    if p.date and p.date not in p.text:
        line = f"{line} ({p.date})"
    return line


def _is_header(p: Paragraph, alias_lookup: dict[str, str], body_size: float | None) -> str | None:
    """Return the canonical section_type if p is a section header, else None."""
    if p.has_num_pr:
        return None
    section_type = alias_lookup.get(_normalise(p.text))
    if section_type is None:
        return None
    elevated = (
        (body_size is not None and p.rendered_size is not None and p.rendered_size > body_size)
        or (p.style_name or "").startswith("Heading")
        or p.is_bold
    )
    return section_type if elevated else None


def detect_headers(
    paras: list[Paragraph],
    section_aliases: dict[str, list[str]],
) -> list[str]:
    """All canonical section_types whose header was matched in the stream.

    Used by ingest.py to reconcile matched headers against emitted sections so an
    empty-but-matched header (e.g. JPMC's "AI Projects") is reported, not silently
    dropped (R-01: no silent drops).
    """
    alias_lookup = _build_alias_lookup(section_aliases)
    body_size = _body_size(paras)
    found: list[str] = []
    for p in paras:
        st = _is_header(p, alias_lookup, body_size)
        if st is not None:
            found.append(st)
    return found


def sectionise(
    paras: list[Paragraph],
    section_aliases: dict[str, list[str]],
    static_sections: set[str] | list[str],
) -> list[ExtractedSection]:
    """Group a CV's paragraphs into ordered ``ExtractedSection`` objects."""
    static = set(static_sections)
    alias_lookup = _build_alias_lookup(section_aliases)
    body_size = _body_size(paras)

    # 1. Split the paragraph stream into (section_type, title, [paragraphs]) blocks.
    #    Everything before the first matched header is the synthesised `header` block.
    #    A matched header with NO body paragraphs is dropped (e.g. JPMC's empty
    #    "AI Projects" — header immediately followed by the next header). This is
    #    intentional: there is nothing to embed, tailor, or assemble. ingest.py's
    #    inventory gate must reconcile matched headers vs emitted sections and
    #    report any empty-but-matched header to the human (R-01: no silent drops).
    blocks: list[tuple[str, str, list[Paragraph]]] = []
    current: tuple[str, str, list[Paragraph]] | None = ("header", "", [])
    for p in paras:
        section_type = _is_header(p, alias_lookup, body_size)
        if section_type is not None:
            if current is not None and current[2]:
                blocks.append(current)
            current = (section_type, p.text, [])
        else:
            current[2].append(p)
    if current is not None and current[2]:
        blocks.append(current)

    # 2. Materialise each block into ExtractedSection(s). Experience fans out per company.
    sections: list[ExtractedSection] = []
    position = 0
    for section_type, title, block_paras in blocks:
        if section_type == "experience":
            position = _emit_experience(block_paras, position, static, sections)
        else:
            position = _emit_simple(section_type, title, block_paras, position, static, sections)
    return sections


def _emit_simple(section_type, title, block_paras, position, static, out) -> int:
    text = "\n".join(_para_text(p) for p in block_paras).strip()
    wc = _word_count(text)
    out.append(ExtractedSection(
        section=CVSection(
            section_id=section_type,
            section_type=section_type,
            position=position,
            static=section_type in static,
            word_count=wc,
            line_count=_line_count(wc),
        ),
        text=text,
        title=title,
    ))
    return position + 1


def _role_title(text: str) -> str:
    """Strip a trailing bracketed date from a role line: 'Director [2021–22]' → 'Director'."""
    return re.sub(r"\s*[\[(].*?[\])]\s*$", "", text).strip()


def _emit_experience(block_paras, position, static, out) -> int:
    """Split the experience block per company AND per role-group (D-21, revised).

    - A **company** line is the largest non-bullet size in the block (14pt here).
    - Within a company, a new **role-group** section starts at a role line (any
      smaller non-bullet line) that *follows a bullet* — i.e. a distinct position
      with its own achievements. Consecutive role lines with no bullet between
      them (stacked promotions sharing bullets, e.g. Director → Associate
      Director) stay in ONE section, since the bullets can't be cleanly divided.
    - section_id = experience_<company>_<first-role-slug>; falls back to
      experience_<company> when a company block has no role line.
    """
    company_size = max(
        (p.rendered_size for p in block_paras if not p.has_num_pr and p.rendered_size is not None),
        default=None,
    )
    company: str | None = None
    titles: list[str] = []        # role title(s) in the current group, in order
    buf: list[Paragraph] = []
    seen_bullet = False
    used_ids: set[str] = set()

    def flush(pos: int) -> int:
        nonlocal buf, titles, seen_bullet
        if company is None or (not buf and not titles):
            buf, titles, seen_bullet = [], [], False
            return pos
        role_slug = _slug(_role_title(titles[0])) if titles else ""
        base = f"experience_{_slug(company)}" + (f"_{role_slug}" if role_slug else "")
        section_id = base
        n = 2
        while section_id in used_ids:        # guard against duplicate role titles
            section_id = f"{base}_{n}"
            n += 1
        used_ids.add(section_id)
        text = "\n".join(_para_text(p) for p in buf).strip()
        wc = _word_count(text)
        out.append(ExtractedSection(
            section=CVSection(
                section_id=section_id,
                section_type="experience",
                position=pos,
                static=False,
                word_count=wc,
                line_count=_line_count(wc),
            ),
            text=text,
            title=(titles[0] if titles else company),
        ))
        buf, titles, seen_bullet = [], [], False
        return pos + 1

    for p in block_paras:
        is_company = (
            not p.has_num_pr
            and company_size is not None
            and p.rendered_size == company_size
        )
        if is_company:
            position = flush(position)
            company = p.text
            continue
        if p.has_num_pr:
            buf.append(p)
            seen_bullet = True
            continue
        # A non-bullet, sub-company line = a role line. Start a new group only if
        # the current group already has bullets (a complete prior position).
        if seen_bullet:
            position = flush(position)
        titles.append(p.text)
        buf.append(p)

    position = flush(position)
    return position
