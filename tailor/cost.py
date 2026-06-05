"""tailor/cost.py — per-model cost estimation (D-08, F-08).

Every provider call flows through helpers.py, so usage is captured there into the
active CostTracker (set by run.py for the duration of a run). The orchestrator and
phases never touch this — it's a side-channel, like the audit log.

Costs are **list-price ESTIMATES**, never a real invoice (F-08): computed as
tokens/1e6 × list rate. Mistral runs on the free tier in practice (£0), so its
figure is what it *would* cost at paid rates. Tracking is at the model level, not
provider level (D-08): demo (Haiku) vs full (Sonnet) must be comparable, which a
single "anthropic" bucket would hide.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["CostTracker", "track", "note", "active", "PRICES_USD_PER_MTOK", "MODEL_KEY"]

# (input, output) USD per 1M tokens — list-price estimates (F-08). Update in one place.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "mistral-small-latest": (0.10, 0.30),   # free tier in practice (F-08)
    "mistral-embed": (0.10, 0.0),
}

# model id → cost_breakdown key (model-level, D-08).
MODEL_KEY: dict[str, str] = {
    "claude-haiku-4-5": "anthropic_haiku",
    "claude-sonnet-4-6": "anthropic_sonnet",
    "gpt-4o-mini": "openai_gpt4o_mini",
    "mistral-small-latest": "mistral_small",
    "mistral-embed": "mistral_small",
}


@dataclass
class CostTracker:
    # model id → [input_tokens, output_tokens]
    tokens: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def note(self, model: str, input_tokens: int, output_tokens: int) -> None:
        t = self.tokens[model]
        t[0] += int(input_tokens or 0)
        t[1] += int(output_tokens or 0)

    def model_usd(self, model: str) -> float:
        pin, pout = PRICES_USD_PER_MTOK.get(model, (0.0, 0.0))
        i, o = self.tokens.get(model, [0, 0])
        return (i / 1e6) * pin + (o / 1e6) * pout

    def breakdown(self) -> dict[str, float]:
        """{cost_breakdown key: estimated USD}, summed across models that map to it."""
        out: dict[str, float] = defaultdict(float)
        for model in self.tokens:
            out[MODEL_KEY.get(model, model)] += self.model_usd(model)
        return {k: round(v, 6) for k, v in sorted(out.items())}

    def total_usd(self) -> float:
        return round(sum(self.model_usd(m) for m in self.tokens), 6)

    def footer(self, *, mode: str, iterations_run: int) -> dict:
        """The §9 run_complete record — explicitly 'estimated', never billed."""
        return {
            "type": "run_complete",
            "cost_breakdown_estimated_usd": self.breakdown(),
            "total_estimated_usd": self.total_usd(),
            "total_estimated_gbp": round(self.total_usd() * 0.79, 6),
            "mode": mode,
            "iterations_run": iterations_run,
            "note": "list-price estimate, not billed; Mistral runs free-tier (F-08)",
        }


# Module-level active tracker — helpers.note() writes here when a run is active.
_active: CostTracker | None = None


def active() -> CostTracker | None:
    return _active


def note(model: str, input_tokens: int, output_tokens: int) -> None:
    """Record usage against the active tracker, if any (no-op outside a run)."""
    if _active is not None:
        _active.note(model, input_tokens, output_tokens)


@contextmanager
def track():
    """Activate a CostTracker for the duration of a run. Nestable (restores prior)."""
    global _active
    tracker, prev = CostTracker(), _active
    _active = tracker
    try:
        yield tracker
    finally:
        _active = prev
