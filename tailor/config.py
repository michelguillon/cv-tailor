"""tailor/config.py — load config.yaml and budgets.yaml into typed objects.

`load_config` is the one place config.yaml is read (corpus + tailor both import
it, so the model/threshold values never drift). `load_budgets` turns the
ingestion-derived budgets.yaml (D-14) into SectionBudget objects keyed by
section_type.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from tailor.models import SectionBudget

__all__ = ["load_config", "load_budgets", "cv_display_name", "RunConfig",
           "resolve_run_config", "ConfigError"]


class ConfigError(RuntimeError):
    pass

CONFIG_PATH = Path("config.yaml")
BUDGETS_PATH = Path("budgets.yaml")


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _stem(filename: str) -> str:
    name = Path(filename).name
    return name[:-5] if name.lower().endswith(".docx") else name


def cv_display_name(config: dict, filename: str | None) -> str:
    """UI/report DISPLAY label for a corpus CV variant (F-41), company-name-free.

    Maps a filename (with/without path or .docx) via ``config['cv_display_names']``;
    falls back to the filename stem when unmapped. Display only — never the stored
    ChromaDB key (retrieval/delete still use the real filename)."""
    if not filename:
        return filename or ""
    stem = _stem(filename)
    mapping = (config or {}).get("cv_display_names") or {}
    for k, v in mapping.items():
        ks = _stem(k)
        # match the full filename ("CV_..._Airwallex") OR the _short_cv display form
        # ("Airwallex", phase1._short_cv strips the personal prefix) without this module
        # needing to know that prefix — a suffix match covers both.
        if ks == stem or ks.endswith("_" + stem):
            return v
    return stem


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


# --------------------------------------------------------------------------- #
# RunConfig — resolved per-run behaviour (D-08, §3.7)                          #
# --------------------------------------------------------------------------- #

@dataclass
class RunConfig:
    """The resolved knobs for one run: which models, how many iterations, the
    convergence thresholds. Mode differences are config values, never `if mode ==`
    branches in the phases (D-08). The orchestrator/writer model swaps Sonnet↔Haiku
    by mode (D-26); the tool models (critique, validation, embeddings) are fixed."""
    mode: str
    orchestrator_model: str       # Claude writer + orchestrator (Haiku/demo, Sonnet/full)
    gpt_model: str                # GPT writer (critique role)
    validation_model: str         # Haiku: formatting gate + HITL free-text interpretation
    jd_model: str                 # Mistral: JD analysis
    embeddings_model: str         # Mistral: retrieval
    max_iterations: int
    cost_cap_usd: float | None
    keyword_delta_threshold: float
    critique_delta_threshold: float
    max_rubric_additions: int


def resolve_run_config(config: dict, *, mode: str = "demo", key: str | None = None,
                       max_iterations: int | None = None) -> RunConfig:
    """Build a RunConfig from config.yaml + the chosen mode. Full mode is key-gated
    (§3.7): the passphrase must match `FULL_MODE_KEY` in the environment (no auth,
    just a guard against an accidental expensive run). Demo needs no key."""
    modes = config.get("modes", {})
    if mode not in modes:
        raise ConfigError(f"unknown mode {mode!r}; available: {sorted(modes)}")
    m = modes[mode]
    if mode == "full":
        required = os.environ.get("FULL_MODE_KEY", "")
        if not required:
            raise ConfigError("full mode requires FULL_MODE_KEY set in the environment (.env)")
        if key != required:
            raise ConfigError("full mode key incorrect — pass --key <passphrase> (or use --demo)")
    models, conv = config["models"], config["convergence"]
    return RunConfig(
        mode=mode,
        orchestrator_model=m["orchestrator_model"],
        gpt_model=models["critique"],
        validation_model=models["validation"],
        jd_model=models["jd_analysis"],
        embeddings_model=models["embeddings"],
        max_iterations=max_iterations or m["max_iterations"],
        cost_cap_usd=m.get("cost_cap_usd"),
        keyword_delta_threshold=conv["keyword_delta_threshold"],
        critique_delta_threshold=conv["critique_delta_threshold"],
        max_rubric_additions=config["rubric"]["max_additions_per_iteration"],
    )
