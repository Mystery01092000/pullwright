"""USD per million tokens. Cache write = 1.25x input. Source: Anthropic pricing, cached 2026-11-03.

Models from other providers are priced under their full name (`openai:gpt-5.1`). Add them with `prices` in a
provider's PULLWRIGHT_PROVIDERS entry, or in a JSON file named by PULLWRIGHT_PRICES
({"openai:gpt-5.1": {"in": 1.25, "out": 10, "cache_read": 0.125}}). An unpriced model counts as $0, and its
usage events say `priced: false` so the cost views can flag it."""
from __future__ import annotations

import json
import os
from pathlib import Path

PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-2": {"in": 8.0, "out": 40.0, "cache_read": 0.20},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0, "cache_read": 0.25},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"in": 5.0, "out": 25.0, "cache_read": 0.50},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_read": 0.10},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_read": 0.01},
}
_env_loaded = False


def register_prices(prices: dict[str, dict[str, float]]) -> None:
    for model, p in prices.items():
        PRICES[model] = {"in": float(p["in"]), "out": float(p["out"]), "cache_read": float(p.get("cache_read", p["in"]))}


def _load_env_prices() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    raw = os.environ.get("PULLWRIGHT_PRICES", "").strip()
    if raw:
        register_prices(json.loads(raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8")))


def price(model: str) -> dict[str, float] | None:
    _load_env_prices()
    if model in PRICES:
        return PRICES[model]
    if model.startswith("anthropic:"):
        return PRICES.get(model.split(":", 1)[1])
    return None


def cost_usd(model: str, usage: dict[str, int]) -> float:
    """usage keys: input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens."""
    p = price(model)
    if p is None:
        return 0.0
    m = 1_000_000
    return (
        usage.get("input_tokens", 0) * p["in"]
        + usage.get("output_tokens", 0) * p["out"]
        + usage.get("cache_creation_input_tokens", 0) * p["in"] * 1.25
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / m
