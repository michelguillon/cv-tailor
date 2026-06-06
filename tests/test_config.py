"""Tests for tailor.config helpers (no API)."""

from tailor.config import cv_display_name

CONFIG = {"cv_display_names": {
    "CV_Michel_Guillon_2026_Airwallex": "CV_Fintech_Solutions_Leadership",
    "CV_Michel_Guillon_2026_AI": "CV_AI_Leadership",
}}


def test_display_name_maps_full_filename_with_and_without_extension():
    assert cv_display_name(CONFIG, "CV_Michel_Guillon_2026_Airwallex.docx") == "CV_Fintech_Solutions_Leadership"
    assert cv_display_name(CONFIG, "CV_Michel_Guillon_2026_Airwallex") == "CV_Fintech_Solutions_Leadership"
    assert cv_display_name(CONFIG, "data/cvs/CV_Michel_Guillon_2026_AI.docx") == "CV_AI_Leadership"


def test_display_name_maps_short_cv_form():
    """phase1._short_cv strips the personal prefix → 'Airwallex'/'AI'; still maps (F-41)."""
    assert cv_display_name(CONFIG, "Airwallex") == "CV_Fintech_Solutions_Leadership"
    assert cv_display_name(CONFIG, "AI") == "CV_AI_Leadership"


def test_display_name_falls_back_to_stem_when_unmapped():
    assert cv_display_name(CONFIG, "CV_A.docx") == "CV_A"      # test-style filename, no leak
    assert cv_display_name(CONFIG, "Unknown_Variant") == "Unknown_Variant"
    assert cv_display_name({}, "anything.docx") == "anything"
    assert cv_display_name(CONFIG, "") == ""


def test_real_config_has_no_company_names_in_values():
    """Guard: the shipped mapping's display labels must not contain target-company names."""
    from tailor.config import load_config
    names = (load_config().get("cv_display_names") or {})
    assert names, "config.yaml should define cv_display_names"
    banned = ["airwallex", "figma", "jpmc", "mistral"]
    for label in names.values():
        assert not any(b in label.lower() for b in banned), f"company name leaked in label: {label}"
