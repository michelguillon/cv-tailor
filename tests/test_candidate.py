"""CVCM loader tests (§3.9 / D-33) — optional candidate value model."""

from tailor.candidate import load_cvcm


def test_absent_file_returns_none(tmp_path):
    assert load_cvcm(tmp_path / "nope.md") is None      # optional: never raises


def test_empty_file_returns_none(tmp_path):
    p = tmp_path / "value_creation_model.md"
    p.write_text("   \n\n", encoding="utf-8")
    assert load_cvcm(p) is None                         # whitespace-only == not provided


def test_present_file_returns_text(tmp_path):
    p = tmp_path / "value_creation_model.md"
    p.write_text("# My value\nI turn ambiguous capability into commercial outcomes.\n", encoding="utf-8")
    out = load_cvcm(p)
    assert out and "commercial outcomes" in out
