"""Step 1 verification (deterministic, no API, no .docx): sectioniser + metadata.

Fixtures are built as `Paragraph` lists directly, mirroring the real corpus
fingerprint (F-05): body 11pt, section headers elevated, companies 14pt, role
lines ≤12pt, bullets via numPr. This pins the section model independently of any
specific .docx file.
"""

import pytest

from corpus.docx_loader import Paragraph
from corpus.metadata import (
    build_metadata,
    sidecar_path,
    sidecar_template,
    validate_sidecar,
)
from corpus.sectioniser import MIN_SECTIONS, ExtractedSection, sectionise

ALIASES = {
    "header": [],
    "profile": ["profile", "summary"],
    "skills": ["core skills", "skills"],
    "experience": ["work experience", "experience"],
    "ai_projects": ["technical & ai projects", "ai projects"],
    "education": ["education"],
    "languages": ["languages"],
    "interests": ["interests"],
}
STATIC = ["header", "education", "languages", "certifications", "interests"]


def body(text, size=11.0, bold=False):
    return Paragraph(text, "Normal", size, bold, False, True)


def header(text, size=16.0, style="Heading 1"):
    return Paragraph(text, style, size, False, False, True)


def company(text, size=14.0, style="Heading 4"):
    return Paragraph(text, style, size, False, False, True)


def role(text, date="", size=11.0, bold=True):
    return Paragraph(text, "Heading 3", size, bold, False, True, date=date)


def bullet(text):
    return Paragraph(text, "List Paragraph", 11.0, False, True, True)


def sample_cv():
    """A compact but representative CV paragraph stream."""
    return [
        Paragraph("Jane Doe", "Normal", 18.0, True, False, False),       # name (header)
        Paragraph("jane@example.com", "Normal", 10.0, False, False, False),  # contact
        header("Profile"),
        body("Principal engineer with 12 years across cloud and platform teams."),
        header("Core Skills", size=14.0, style="Heading 4"),             # smaller header
        body("Cloud architecture · Kubernetes · Stakeholder management"),
        header("Work experience"),
        company("Acme Corp"),
        role("Principal Engineer", date="2022-present"),
        bullet("Led platform re-architecture, cutting latency 40%."),
        company("Globex"),
        role("Senior Engineer", date="2019-2022"),
        bullet("Owned the billing service."),
        bullet("Mentored four engineers."),
        header("Education"),
        body("MSc Computer Science, Imperial College (2011)", bold=True),
        header("Languages"),
        body("English, French"),
        header("Interests"),
        body("Cycling, woodworking"),
    ]


@pytest.fixture
def sections():
    return sectionise(sample_cv(), ALIASES, STATIC)


def ids(sections):
    return [es.section.section_id for es in sections]


def by_id(sections, section_id):
    return next(es for es in sections if es.section.section_id == section_id)


# --------------------------------------------------------------------------- #
# Section detection                                                           #
# --------------------------------------------------------------------------- #

def test_all_canonical_sections_detected(sections):
    assert ids(sections) == [
        "header",
        "profile",
        "skills",
        "experience_acme_corp_principal_engineer",
        "experience_globex_senior_engineer",
        "education",
        "languages",
        "interests",
    ]


def test_positions_are_sequential(sections):
    assert [es.section.position for es in sections] == list(range(len(sections)))


def test_above_minimum_section_floor(sections):
    assert len(sections) >= MIN_SECTIONS


def test_header_block_synthesised_from_pre_profile(sections):
    h = by_id(sections, "header")
    assert "Jane Doe" in h.text
    assert "jane@example.com" in h.text
    assert h.section.static is True


def test_smaller_styled_header_still_detected(sections):
    """'Core Skills' is Heading 4 / 14pt — must still be a section header (F-04)."""
    skills = by_id(sections, "skills")
    assert "Kubernetes" in skills.text
    assert skills.section.section_type == "skills"


def test_static_flags(sections):
    static_ids = {es.section.section_id for es in sections if es.section.static}
    assert static_ids == {"header", "education", "languages", "interests"}
    assert by_id(sections, "profile").section.static is False
    assert by_id(sections, "experience_acme_corp_principal_engineer").section.static is False


# --------------------------------------------------------------------------- #
# Experience company + role-group split (D-21 revised)                        #
# --------------------------------------------------------------------------- #

def test_experience_splits_per_company(sections):
    exp = [es for es in sections if es.section.section_type == "experience"]
    assert [es.section.section_id for es in exp] == [
        "experience_acme_corp_principal_engineer",
        "experience_globex_senior_engineer",
    ]


def test_role_and_bullets_attach_to_company(sections):
    acme = by_id(sections, "experience_acme_corp_principal_engineer")
    assert "Principal Engineer" in acme.text
    assert "2022-present" in acme.text            # date paired in
    assert "Led platform re-architecture" in acme.text
    globex = by_id(sections, "experience_globex_senior_engineer")
    assert "Mentored four engineers" in globex.text
    assert "Acme" not in globex.text              # company boundary respected


def test_bullets_rendered_as_list(sections):
    acme = by_id(sections, "experience_acme_corp_principal_engineer")
    assert "- Led platform re-architecture" in acme.text


def test_role_after_bullets_starts_new_section():
    """A second role with its own bullets becomes its own section (per-role split)."""
    paras = [
        header("Work experience"),
        company("Acme"),
        role("Senior Engineer", date="2020-2022"),
        bullet("Did A."),
        role("Staff Engineer", date="2022-2024"),   # follows a bullet → new section
        bullet("Did B."),
    ]
    result = sectionise(paras, ALIASES, STATIC)
    exp_ids = [es.section.section_id for es in result if es.section.section_type == "experience"]
    assert exp_ids == ["experience_acme_senior_engineer", "experience_acme_staff_engineer"]
    senior = next(es for es in result if es.section.section_id == "experience_acme_senior_engineer")
    assert "Did A." in senior.text and "Did B." not in senior.text


def test_stacked_promotions_sharing_bullets_stay_together():
    """Consecutive role lines with no bullet between them are one section (shared bullets)."""
    paras = [
        header("Work experience"),
        company("Appnexus"),
        role("Director", date="2021-2022"),
        role("Associate Director", date="2019-2021"),   # no bullet since last role → same group
        bullet("Scaled the team."),
        bullet("Owned strategy."),
    ]
    result = sectionise(paras, ALIASES, STATIC)
    exp = [es for es in result if es.section.section_type == "experience"]
    assert len(exp) == 1
    assert exp[0].section.section_id == "experience_appnexus_director"
    assert "Director" in exp[0].text and "Associate Director" in exp[0].text
    assert "Scaled the team." in exp[0].text


# --------------------------------------------------------------------------- #
# Counts                                                                       #
# --------------------------------------------------------------------------- #

def test_word_and_line_counts(sections):
    profile = by_id(sections, "profile").section
    assert profile.word_count == len(
        "Principal engineer with 12 years across cloud and platform teams.".split()
    )
    assert profile.line_count >= 1


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #

def test_empty_matched_header_is_dropped():
    """A matched header with no body (e.g. JPMC's empty 'AI Projects') is dropped."""
    paras = [
        header("Profile"),
        body("A short profile."),
        header("AI Projects"),       # immediately followed by next header — no body
        header("Interests"),
        body("Reading"),
    ]
    result = sectionise(paras, ALIASES, STATIC)
    assert "ai_projects" not in [es.section.section_id for es in result]


def test_accent_and_punctuation_normalised():
    paras = [
        header("Profile"),
        body("x"),
        header("Éducation"),         # accented + would-be punctuation
        body("Some degree"),
    ]
    result = sectionise(paras, {"profile": ["profile"], "education": ["education"]}, STATIC)
    assert "education" in [es.section.section_id for es in result]


def test_body_paragraph_matching_alias_text_is_not_a_header():
    """A non-elevated body line equal to an alias must not split a section."""
    paras = [
        header("Profile"),
        body("profile"),             # same text, but body-sized, non-bold → not a header
        body("more profile text"),
    ]
    result = sectionise(paras, {"profile": ["profile"]}, STATIC)
    assert [es.section.section_id for es in result] == ["profile"]
    assert "more profile text" in result[0].text


# --------------------------------------------------------------------------- #
# Metadata sidecar                                                            #
# --------------------------------------------------------------------------- #

def test_sidecar_path():
    assert sidecar_path("data/cvs/CV_X.docx").name == "CV_X.yaml"


def test_build_metadata_fuses_sidecar_and_sections(tmp_path, sections):
    docx = tmp_path / "CV_X.docx"
    docx.write_bytes(b"")  # not parsed here — build_metadata reads the sidecar
    (tmp_path / "CV_X.yaml").write_text(
        "filename: CV_X.docx\n"
        "cv_type: job_specific\n"
        "target_role: Solutions Engineer\n"
        "target_company: Acme\n"
        "skills_emphasis: [cloud, kubernetes]\n"
        "seniority: principal\n"
        "version_date: 2026-05-01\n",
        encoding="utf-8",
    )
    meta = build_metadata(docx, [es.section for es in sections])
    assert meta.target_role == "Solutions Engineer"
    assert meta.target_company == "Acme"
    assert meta.skills_emphasis == ["cloud", "kubernetes"]
    assert len(meta.sections) == len(sections)
    assert meta.sections[0].section_id == "header"


def test_build_metadata_missing_sidecar_raises(tmp_path):
    docx = tmp_path / "CV_Y.docx"
    docx.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        build_metadata(docx, [])


def test_sidecar_template_includes_required_fields():
    tpl = sidecar_template("CV_Michel_Guillon_2026_Airwallex.docx")
    for field in ("filename:", "cv_type:", "target_role:", "seniority:", "version_date:"):
        assert field in tpl
    assert "Airwallex" in tpl  # filename-derived hint


# --------------------------------------------------------------------------- #
# Sidecar validation (R-09) — catches the real mistakes at write-time         #
# --------------------------------------------------------------------------- #

def _good_sidecar():
    return {
        "filename": "CV_X.docx",
        "cv_type": "job_specific",
        "target_role": "Solutions Engineer",
        "target_company": "Acme",
        "skills_emphasis": ["cloud"],
        "seniority": "principal",
        "version_date": "2026-05-01",
    }


def test_valid_sidecar_has_no_errors():
    errors, warnings = validate_sidecar(_good_sidecar())
    assert errors == [] and warnings == []


def test_multi_value_seniority_is_an_error():
    """'principal, director, VP' parses as one string and must be rejected."""
    d = _good_sidecar() | {"seniority": "principal, director, VP"}
    errors, _ = validate_sidecar(d)
    assert any("seniority" in e for e in errors)


def test_unknown_seniority_is_an_error():
    errors, _ = validate_sidecar(_good_sidecar() | {"seniority": "VP"})
    assert any("seniority" in e for e in errors)


def test_vp_is_accepted():
    errors, _ = validate_sidecar(_good_sidecar() | {"seniority": "vp"})
    assert errors == []


def test_bad_cv_type_is_an_error():
    errors, _ = validate_sidecar(_good_sidecar() | {"cv_type": "generic-ish"})
    assert any("cv_type" in e for e in errors)


def test_skills_emphasis_must_be_list():
    errors, _ = validate_sidecar(_good_sidecar() | {"skills_emphasis": "cloud, ai"})
    assert any("skills_emphasis" in e for e in errors)


def test_generic_with_company_is_a_warning_not_error():
    d = _good_sidecar() | {"cv_type": "generic", "target_company": "Adtech Consulting"}
    errors, warnings = validate_sidecar(d)
    assert errors == []
    assert any("target_company" in w for w in warnings)


def test_missing_fields_reported():
    errors, _ = validate_sidecar({"filename": "x.docx"})
    assert any("missing field" in e for e in errors)


# --------------------------------------------------------------------------- #
# ingest / retrieval pure logic (no API, no ChromaDB)                         #
# --------------------------------------------------------------------------- #

def test_sanitise_metadata_strips_none_and_empty():
    from corpus.ingest import sanitise_metadata
    out = sanitise_metadata({"a": "x", "target_company": None, "b": "", "n": 0, "ok": False})
    assert out == {"a": "x", "n": 0, "ok": False}   # None/"" dropped; 0/False kept


def test_derive_budgets_min_max_median():
    import types
    from corpus.ingest import derive_budgets
    from tailor.models import CVSection

    def es(section_type, wc):
        sec = CVSection(section_type, section_type, 0, word_count=wc)
        return ExtractedSection(section=sec, text="x " * wc, title="t")

    cvs = [
        types.SimpleNamespace(sections=[es("profile", 70), es("experience", 120)]),
        types.SimpleNamespace(sections=[es("profile", 115), es("experience", 90), es("experience", 200)]),
    ]
    budgets = derive_budgets(cvs)
    assert budgets["profile"] == {"min_words": 70, "max_words": 115, "target_words": 92}
    assert budgets["experience"]["min_words"] == 90
    assert budgets["experience"]["max_words"] == 200
    assert budgets["experience"]["target_words"] == 120   # median of [120, 90, 200]


def test_build_where_filters():
    from corpus.retrieval import build_where
    assert build_where() is None
    assert build_where(section_type="experience") == {"section_type": "experience"}
    assert build_where(section_type="experience", cv_type="generic") == {
        "$and": [{"section_type": "experience"}, {"cv_type": "generic"}]
    }


def test_build_where_excludes_seniority():
    """D-23: seniority is never a hard filter — build_where takes no seniority arg."""
    import inspect
    from corpus.retrieval import build_where
    assert "seniority" not in inspect.signature(build_where).parameters


def test_get_collection_reuses_one_client(monkeypatch):
    """ChromaDB 1.x (Rust bindings) breaks when a path gets multiple PersistentClients in one
    process — the cause of the deployment's Corpus-tab 500s (F-49). get_collection must build a
    client once per persist_dir and reuse it, even across threads."""
    import threading as _threading
    import types

    import chromadb

    from corpus import ingest

    constructed = []

    class _FakeClient:
        def __init__(self, path):
            constructed.append(path)
        def get_or_create_collection(self, name, metadata=None):
            return types.SimpleNamespace(metadata={"hnsw:space": "cosine"})

    monkeypatch.setattr(chromadb, "PersistentClient", lambda path: _FakeClient(path))
    monkeypatch.setattr(ingest, "_CLIENT_CACHE", {})          # isolate from other tests/process state
    cfg = {"chroma": {"persist_dir": "data/chroma", "collection": "cv_sections_cosine",
                      "metric": "cosine"}}

    cols = []
    threads = [_threading.Thread(target=lambda: cols.append(ingest.get_collection(cfg)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert constructed == ["data/chroma"]                     # exactly one client, despite 8 concurrent calls
    assert len(cols) == 8
