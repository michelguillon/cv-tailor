"""tailor/config.py — load config.yaml and budgets.yaml into typed objects.

`load_config` is the one place config.yaml is read (corpus + tailor both import
it, so the model/threshold values never drift). `load_budgets` turns the
ingestion-derived budgets.yaml (D-14) into SectionBudget objects keyed by
section_type.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tailor.models import SectionBudget

__all__ = ["load_config", "load_budgets"]

CONFIG_PATH = Path("config.yaml")
BUDGETS_PATH = Path("budgets.yaml")


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_budgets(path: str | Path = BUDGETS_PATH) -> dict[str, SectionBudget]:
    """Read budgets.yaml → {section_type: SectionBudget}. Raises if not yet derived."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Run `python -m corpus.ingest --cv-dir data/cvs/` first; "
            f"budgets are derived from the corpus (D-14)."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        st: SectionBudget(section_type=st, **vals)
        for st, vals in data.items()
    }
